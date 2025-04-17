#!/usr/bin/env python3
"""
Realtime speech‑to‑speech for STR.

• Streams USB‑mic audio (with optional normalisation) or typed text
  to OpenAI Realtime API and plays voice replies through the DAC.
• Push‑button + LED on Raspberry Pi toggle recording in hardware.
• Web‑UI “Start/Stop” buttons work in parallel.

Electrical wiring (BCM numbering):

  ┌────────────────────────────┐
  │ 10 kΩ pull‑down resistor    │
  │   (GPIO‑>GND)              │
  └────────┬───────────────────┘
           │
3 V3 ─────►┴───┐
               │   Push‑button
GPIO17 ◄───────┘
               │
GND  ──────────┘

LED anode  ──► 330 Ω resistor ─► GPIO27  
LED cathode ──► GND
"""

from __future__ import annotations

import asyncio, base64, json, logging, os, ssl, threading
from typing import Callable, Optional

import pyaudio, numpy as np, websockets           # websockets==13.*

from config import (
    MIC_DEVICE_INDEX,
    MIC_SAMPLE_RATE,
    MIC_CHANNELS,
    MIC_CHUNK,
    MIC_NORMALISE,
    DAC_APLAY_DEVICE,
    GPIO_BUTTON_PIN,
    GPIO_LED_PIN,
    BUTTON_ACTIVE_HIGH,
)

# ------------------------------------------------------------------#
#  Logging                                                          #
# ------------------------------------------------------------------#
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)5s | %(name)s | %(message)s",
)
log = logging.getLogger("realtime")

# ------------------------------------------------------------------#
#  Prompt                                                           #
# ------------------------------------------------------------------#
INSTRUCTIONS = (
    "You are a professional radio broadcaster. Provide a natural, "
    "broadcast‑style answer. Answer in Spanish from Spain. Use European "
    "format for all dates and units. Do not say anything in your first "
    "message except 'Voice real time mode started.'. Answer briefly."
)

# ------------------------------------------------------------------#
#  Audio handler                                                    #
# ------------------------------------------------------------------#
class AudioHandler:
    """USB‑mic capture and local speaker playback."""

    def __init__(self, device_index: int):
        self.device_index = device_index
        self.channels = MIC_CHANNELS
        self.chunk = MIC_CHUNK
        self.fmt = pyaudio.paInt16

        self.p = pyaudio.PyAudio()

        # Query mic’s native rate if not forced
        if MIC_SAMPLE_RATE == 0:
            info = self.p.get_device_info_by_index(self.device_index)
            self.rate = int(info["defaultSampleRate"])
        else:
            self.rate = MIC_SAMPLE_RATE

        self.stream_in = None
        self._rec = False

        for i in range(self.p.get_device_count()):
            info = self.p.get_device_info_by_index(i)
            if info["maxInputChannels"]:
                log.debug("Input‑ID %d : %s", i, info["name"])

    # ---------- mic ---------- #
    def start(self):
        if self.stream_in:
            self.stop()
        self.stream_in = self.p.open(
            format=self.fmt,
            channels=self.channels,
            rate=self.rate,
            input=True,
            frames_per_buffer=self.chunk,
            input_device_index=self.device_index,
        )
        self._rec = True
        log.info("🎙️  Mic ON (idx=%d, %d Hz)", self.device_index, self.rate)

    def read(self) -> bytes | None:
        if not self._rec or not self.stream_in:
            return None
        try:
            data = self.stream_in.read(self.chunk, exception_on_overflow=False)
            if MIC_NORMALISE:
                audio = np.frombuffer(data, np.int16)
                peak = np.max(np.abs(audio)) or 1
                gain = int(0.9 * 32767 / peak)
                if gain > 1:
                    audio = np.clip(audio * gain, -32768, 32767).astype(np.int16)
                    data = audio.tobytes()
            return data
        except Exception as exc:  # noqa: BLE001
            log.error("Mic read error: %s", exc)
            return None

    def stop(self):
        if self.stream_in:
            self.stream_in.stop_stream()
            self.stream_in.close()
            self.stream_in = None
        self._rec = False
        log.info("🎙️  Mic OFF")

    # ---------- speaker ---------- #
    def play(self, pcm16: bytes):
        def _play():
            try:
                out = self.p.open(
                    format=self.fmt, channels=1, rate=self.rate, output=True
                )
                out.write(pcm16)
                out.stop_stream()
                out.close()
            except Exception as exc:  # noqa: BLE001
                log.error("Speaker error: %s", exc)

        threading.Thread(target=_play, daemon=True).start()

    # ---------- cleanup ---------- #
    def close(self):
        self.stop()
        self.p.terminate()


# ------------------------------------------------------------------#
#  Realtime client                                                  #
# ------------------------------------------------------------------#
class RealtimeClient:
    """Streams audio/text to OpenAI Realtime API."""

    _URL = "wss://api.openai.com/v1/realtime"
    _MODEL = "gpt-4o-mini-realtime-preview"

    def __init__(
        self,
        instructions: str,
        voice: str = "ash",
        mic_index: int = MIC_DEVICE_INDEX,
        on_text: Optional[Callable[[str], None]] = None,
    ):
        self.api_key = os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY missing")

        self.voice = voice
        self.instructions = instructions
        self.on_text = on_text

        self.audio = AudioHandler(mic_index)
        self._audio_buf = b""
        self._text_buf = ""
        self._rec_flag = threading.Event()

        # Async event‑loop in background
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

        # Connect WebSocket
        self.ws = asyncio.run_coroutine_threadsafe(
            self._connect(), self.loop
        ).result()
        asyncio.run_coroutine_threadsafe(self._recv(), self.loop)

        # GPIO
        self._setup_gpio()

    # ---------------- WebSocket ------------------------------------#
    async def _connect(self):
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        self.ws = await websockets.connect(
            f"{self._URL}?model={self._MODEL}",
            extra_headers={
                "Authorization": f"Bearer {self.api_key}",
                "OpenAI-Beta": "realtime=v1",
            },
            ssl=ssl_ctx,
        )
        await self._send(
            {
                "type": "session.update",
                "session": {
                    "modalities": ["audio", "text"],
                    "instructions": self.instructions,
                    "voice": self.voice,
                    "input_audio_format": "pcm16",
                    "output_audio_format": "pcm16",
                    "turn_detection": None,
                    "input_audio_transcription": {"model": "whisper-1"},
                    "temperature": 0.6,
                },
            }
        )
        await self._send({"type": "response.create"})
        log.info("Realtime session ready")
        return self.ws

    async def _send(self, ev: dict):
        await self.ws.send(json.dumps(ev))

    async def _recv(self):
        try:
            async for msg in self.ws:
                await self._handle(json.loads(msg))
        except Exception as exc:  # noqa: BLE001
            log.error("WS receive loop exited: %s", exc)

    async def _handle(self, ev: dict):
        typ = ev.get("type")
        if typ == "error":
            log.error("API error: %s", ev["error"]["message"])
        elif typ == "response.text.delta":
            self._text_buf += ev["delta"]
        elif typ == "response.text.done":
            if self.on_text:
                self.on_text(self._text_buf)
            self._text_buf = ""
        elif typ == "response.audio.delta":
            self._audio_buf += base64.b64decode(ev["delta"])
        elif typ == "response.audio.done":
            if self._audio_buf:
                self.audio.play(self._audio_buf)
            self._audio_buf = b""
        else:
            log.debug("Event %s", typ)

    # ---------------- mic streaming --------------------------------#
    async def _mic_loop(self):
        self.audio.start()
        try:
            while self._rec_flag.is_set():
                chunk = self.audio.read()
                if chunk:
                    await self._send(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(chunk).decode(),
                        }
                    )
                await asyncio.sleep(0.0)
        finally:
            self.audio.stop()
            await self._send({"type": "input_audio_buffer.commit"})
            await self._send({"type": "response.create"})
            log.info("Audio committed")

    # ---------------- text -----------------------------------------#
    async def _send_text_async(self, text: str):
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        await self._send({"type": "response.create"})

    # ---------------- GPIO setup -----------------------------------#
    def _setup_gpio(self):
        try:
            import RPi.GPIO as GPIO  # type: ignore
        except (ImportError, RuntimeError):
            log.warning("GPIO not available – running headless dev mode?")
            self.gpio_ok = False
            return

        self.gpio_ok = True
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(GPIO_LED_PIN, GPIO.OUT, initial=GPIO.LOW)

        pull = GPIO.PUD_DOWN if BUTTON_ACTIVE_HIGH else GPIO.PUD_UP
        GPIO.setup(GPIO_BUTTON_PIN, GPIO.IN, pull_up_down=pull)

        edge = GPIO.RISING if BUTTON_ACTIVE_HIGH else GPIO.FALLING
        GPIO.add_event_detect(
            GPIO_BUTTON_PIN, edge, callback=self._gpio_toggle, bouncetime=150
        )
        log.info(
            "GPIO ready (button GPIO%d active %s, LED GPIO%d)",
            GPIO_BUTTON_PIN,
            "HIGH" if BUTTON_ACTIVE_HIGH else "LOW",
            GPIO_LED_PIN,
        )

    def _gpio_toggle(self, channel=None):
        import RPi.GPIO as GPIO  # type: ignore
        if self._rec_flag.is_set():
            self.stop_talking()
            if self.gpio_ok:
                GPIO.output(GPIO_LED_PIN, GPIO.LOW)
        else:
            self.start_talking()
            if self.gpio_ok:
                GPIO.output(GPIO_LED_PIN, GPIO.HIGH)

    # ---------------- public API -----------------------------------#
    def start_talking(self):
        if self._rec_flag.is_set():
            return
        self._rec_flag.set()
        asyncio.run_coroutine_threadsafe(self._mic_loop(), self.loop)
        log.debug("Recording started")

    def stop_talking(self):
        self._rec_flag.clear()
        log.debug("Recording stopped")

    def send_text(self, text: str):
        asyncio.run_coroutine_threadsafe(
            self._send_text_async(text), self.loop
        )

    # ---------------- cleanup --------------------------------------#
    def close(self):
        if self.gpio_ok:
            import RPi.GPIO as GPIO  # type: ignore
            GPIO.output(GPIO_LED_PIN, GPIO.LOW)
            GPIO.cleanup((GPIO_BUTTON_PIN, GPIO_LED_PIN))
        self.audio.close()
        if self.ws and not self.ws.closed:
            asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop)
        self.loop.call_soon_threadsafe(self.loop.stop())
