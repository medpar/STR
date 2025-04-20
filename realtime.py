#!/usr/bin/env python3
"""
Realtime speech-to-speech for STR.

• Records from USB mic at 48 kHz, downsamples to 24 kHz for OpenAI streaming.
• Streams audio or typed text to OpenAI Realtime API and plays back at OUTPUT_SAMPLE_RATE Hz stereo via the PCM5102 DAC.
• Toggles recording via web buttons or physical push‑button (GPIO17), lights LED (GPIO27) while recording.
• Uses external 10 kΩ pull‑down resistor on the button when available.
"""

from __future__ import annotations
import os
import ssl
import json
import base64
import asyncio
import threading
import logging
import pyaudio
import numpy as np
import websockets

# GPIO import (optional)
try:
    import RPi.GPIO as GPIO
    HAS_GPIO = True
except ImportError:
    HAS_GPIO = False

from config import (
    MIC_DEVICE_INDEX,
    MIC_SAMPLE_RATE,
    MIC_CHANNELS,
    MIC_CHUNK,
    MIC_NORMALISE,
    GPIO_BUTTON_PIN,
    GPIO_LED_PIN,
    BUTTON_ACTIVE_HIGH,
    OPENAI_MODEL_REALTIME,
    OPENAI_MODEL_TRANSCRIPTION,
    OUTPUT_SAMPLE_RATE,
    DAC_PYAUDIO_INDEX,
    PLAYBACK_CHUNK,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)5s | %(name)s | %(message)s",
)
log = logging.getLogger("realtime")

API_SAMPLE_RATE = 24000  # OpenAI Realtime expects 24 kHz

INSTRUCTIONS = (
    "You are a professional radio broadcaster. Provide a natural, "
    "broadcast-style answer. Answer in Spanish from Spain. Use European "
    "format for all dates and units. Do not say anything in your first "
    "message except 'Started'. Answer briefly."
)


class AudioHandler:
    """Handle mic capture at MIC_SAMPLE_RATE and playback at OUTPUT_SAMPLE_RATE stereo."""

    def __init__(self, device_index: int):
        self.device_index = device_index
        self.channels = MIC_CHANNELS
        self.chunk = MIC_CHUNK
        self.fmt = pyaudio.paInt16
        self.p = pyaudio.PyAudio()

        if MIC_SAMPLE_RATE and MIC_SAMPLE_RATE != 0:
            self.input_rate = MIC_SAMPLE_RATE
        else:
            info = self.p.get_device_info_by_index(self.device_index)
            self.input_rate = int(info["defaultSampleRate"])

        log.info("Mic idx=%d, input rate=%d Hz", self.device_index, self.input_rate)
        self.stream = None
        self.recording = False

    def start_input(self):
        if self.stream:
            self.stop_input()
        self.stream = self.p.open(
            format=self.fmt,
            channels=self.channels,
            rate=self.input_rate,
            input=True,
            frames_per_buffer=self.chunk,
            input_device_index=self.device_index,
        )
        self.recording = True
        log.info("🎙️ Mic ON")

    def read_chunk(self) -> bytes | None:
        if not self.recording or not self.stream:
            return None
        try:
            raw = self.stream.read(self.chunk, exception_on_overflow=False)
        except Exception as e:
            log.error("Mic read error: %s", e)
            return None

        audio = np.frombuffer(raw, dtype=np.int16)
        if MIC_NORMALISE:
            peak = np.max(np.abs(audio)) or 1
            gain = int(0.9 * 32767 / peak)
            if gain > 1:
                audio = np.clip(audio * gain, -32768, 32767).astype(np.int16)

        if self.input_rate != API_SAMPLE_RATE:
            factor = self.input_rate // API_SAMPLE_RATE
            if factor > 1:
                audio = audio[::factor]

        return audio.tobytes()

    def stop_input(self):
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None
        self.recording = False
        log.info("🎙️ Mic OFF")

    def play(self, data: bytes):
        """Play API response audio: resample from 24 kHz mono to OUTPUT_SAMPLE_RATE stereo."""

        def _playback():
            try:
                # Load mono samples
                samples = np.frombuffer(data, dtype=np.int16)
                in_rate = API_SAMPLE_RATE
                out_rate = OUTPUT_SAMPLE_RATE

                # Resample if rates differ
                if in_rate != out_rate:
                    orig_idx = np.arange(len(samples))
                    target_len = int(len(samples) * out_rate / in_rate)
                    new_idx = np.linspace(0, len(samples) - 1, target_len)
                    samples = np.interp(new_idx, orig_idx, samples).astype(np.int16)

                # Mono to stereo
                stereo = np.repeat(samples, 2)

                # Open playback stream
                stream = self.p.open(
                    format=self.fmt,
                    channels=2,
                    rate=out_rate,
                    output=True,
                    output_device_index=DAC_PYAUDIO_INDEX,
                    frames_per_buffer=PLAYBACK_CHUNK,
                )
                stream.write(stereo.tobytes())
                stream.stop_stream()
                stream.close()
            except Exception as e:
                log.error("Playback error: %s", e)

        threading.Thread(target=_playback, daemon=True).start()

    def close(self):
        self.stop_input()
        self.p.terminate()


class RealtimeClient:
    URL = "wss://api.openai.com/v1/realtime"
    MODEL = OPENAI_MODEL_REALTIME

    def __init__(
        self,
        instructions: str,
        voice: str = "ash",
        mic_index: int = MIC_DEVICE_INDEX,
        on_text: Callable[[str], None] | None = None,
    ):
        self.api_key = os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY not set")

        self.instructions = instructions
        self.voice = voice
        self.on_text = on_text

        self.audio = AudioHandler(mic_index)
        self._audio_buf = b""
        self._text_buf = ""
        self._rec_flag = threading.Event()

        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

        self.ws = asyncio.run_coroutine_threadsafe(self._connect(), self.loop).result()
        asyncio.run_coroutine_threadsafe(self._recv_loop(), self.loop)

        if HAS_GPIO:
            self._setup_gpio()
        else:
            log.info("GPIO not available; hardware button disabled")

    async def _connect(self):
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        ws = await websockets.connect(
            f"{self.URL}?model={self.MODEL}",
            extra_headers={
                "Authorization": f"Bearer {self.api_key}",
                "OpenAI-Beta": "realtime=v1",
            },
            ssl=ssl_ctx,
        )
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "modalities": ["audio", "text"],
                "instructions": self.instructions,
                "voice": self.voice,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "turn_detection": None,
                "input_audio_transcription": {"model": OPENAI_MODEL_TRANSCRIPTION},
                "temperature": 0.6,
            }
        }))
        await ws.send(json.dumps({"type": "response.create"}))
        log.info("WebSocket session ready")
        return ws

    async def _recv_loop(self):
        try:
            async for message in self.ws:
                await self._handle(json.loads(message))
        except Exception as e:
            log.error("WebSocket receive error: %s", e)

    async def _handle(self, ev: dict):
        t = ev.get("type")
        if t == "error":
            log.error("API error: %s", ev["error"]["message"])
        elif t == "response.text.delta":
            self._text_buf += ev["delta"]
        elif t == "response.text.done":
            if self.on_text:
                self.on_text(self._text_buf)
            self._text_buf = ""
        elif t == "response.audio.delta":
            self._audio_buf += base64.b64decode(ev["delta"])
        elif t == "response.audio.done":
            if self._audio_buf:
                self.audio.play(self._audio_buf)
            self._audio_buf = b""

    async def _mic_stream(self):
        self.audio.start_input()
        while self._rec_flag.is_set():
            chunk = self.audio.read_chunk()
            if chunk:
                await self.ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode(),
                }))
            await asyncio.sleep(0.0)
        self.audio.stop_input()
        await self.ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        await self.ws.send(json.dumps({"type": "response.create"}))
        log.info("Audio committed")

    async def _send_text_async(self, text: str):
        await self.ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            }
        }))
        await self.ws.send(json.dumps({"type": "response.create"}))

    def start_talking(self):
        if not self._rec_flag.is_set():
            self._rec_flag.set()
            asyncio.run_coroutine_threadsafe(self._mic_stream(), self.loop)
            log.info("Started talking")

    def stop_talking(self):
        if self._rec_flag.is_set():
            self._rec_flag.clear()
            log.info("Stopped talking")

    def send_text(self, text: str):
        asyncio.run_coroutine_threadsafe(self._send_text_async(text), self.loop)

    def close(self):
        self.audio.close()
        if self.ws and not self.ws.closed:
            asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop)
        self.loop.call_soon_threadsafe(self.loop.stop())
        if HAS_GPIO:
            GPIO.cleanup((GPIO_BUTTON_PIN, GPIO_LED_PIN))

    def _setup_gpio(self):
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(GPIO_LED_PIN, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(GPIO_BUTTON_PIN, GPIO.IN)
        threading.Thread(target=self._poll_button, daemon=True).start()
        log.info("GPIO polling thread started (button %d, LED %d)", GPIO_BUTTON_PIN, GPIO_LED_PIN)

    def _poll_button(self):
        last = GPIO.input(GPIO_BUTTON_PIN)
        while True:
            cur = GPIO.input(GPIO_BUTTON_PIN)
            if not last and cur:
                if self._rec_flag.is_set():
                    self.stop_talking()
                    GPIO.output(GPIO_LED_PIN, GPIO.LOW)
                else:
                    self.start_talking()
                    GPIO.output(GPIO_LED_PIN, GPIO.HIGH)
            last = cur
            threading.Event().wait(0.05)
