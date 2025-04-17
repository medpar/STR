#!/usr/bin/env python3
"""
Realtime speech‑to‑speech for STR.

• Records from the USB mic defined in *config.py* (with chunk‑wise
  normalisation) and streams to OpenAI Realtime API.
• Works on macOS for dev and on Raspberry Pi with a physical push‑button
  + LED (GPIO17 / GPIO27). Web buttons still work.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import ssl
import threading
from typing import Callable, Optional

import pyaudio
import websockets          # ==13.*
import numpy as np          # normalisation maths

from config import (
    MIC_DEVICE_INDEX,
    SAMPLE_RATE,
    NUM_CHANNELS,
    FRAME_CHUNK,
    NORMALISE_INPUT,
    GPIO_BUTTON_PIN,
    GPIO_LED_PIN,
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
#  GPIO (optional)                                                  #
# ------------------------------------------------------------------#
try:
    from gpiozero import Button, LED  # type: ignore
    _gpio_ok = True
except (ImportError, RuntimeError):
    _gpio_ok = False
    Button = LED = None  # type: ignore


# ------------------------------------------------------------------#
#  Audio handler                                                    #
# ------------------------------------------------------------------#
class AudioHandler:
    """USB microphone capture and speaker playback."""

    def __init__(self, device_index: int | None = None):
        self.rate = SAMPLE_RATE
        self.chunk = FRAME_CHUNK
        self.channels = NUM_CHANNELS
        self.fmt = pyaudio.paInt16
        self.device_index = device_index

        self.p = pyaudio.PyAudio()
        self.stream_in = None
        self._recording = False

        for i in range(self.p.get_device_count()):
            dev = self.p.get_device_info_by_index(i)
            if dev["maxInputChannels"]:
                log.debug("Input‑ID %d : %s", i, dev["name"])

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
        self._recording = True
        log.info("🎙️  Mic ON (device %s, %d Hz)", self.device_index, self.rate)

    def read_chunk(self) -> bytes | None:
        if not self._recording or not self.stream_in:
            return None
        try:
            data = self.stream_in.read(
                self.chunk, exception_on_overflow=False
            )
            if NORMALISE_INPUT:
                data = self._normalise(data)
            return data
        except Exception as exc:  # noqa: BLE001
            log.error("Mic read error: %s", exc)
            return None

    def _normalise(self, raw: bytes) -> bytes:
        """Boost audio so its peak reaches 90 % full‑scale."""
        audio = np.frombuffer(raw, np.int16)
        if audio.size == 0:
            return raw
        peak = np.max(np.abs(audio))
        if peak == 0:
            return raw
        gain = int(0.9 * 32767 / peak)
        if gain > 1:
            audio = np.clip(audio * gain, -32768, 32767).astype(np.int16)
        return audio.tobytes()

    def stop(self):
        if self.stream_in:
            self.stream_in.stop_stream()
            self.stream_in.close()
            self.stream_in = None
        self._recording = False
        log.info("🎙️  Mic OFF")

    # ---------- speaker ---------- #
    def play(self, pcm16_audio: bytes):
        def _play():
            try:
                out = self.p.open(
                    format=self.fmt,
                    channels=1,
                    rate=self.rate,
                    output=True,
                )
                out.write(pcm16_audio)
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
    """Streams mic or typed text to OpenAI Realtime API."""

    _URL = "wss://api.openai.com/v1/realtime"
    _MODEL = "gpt-4o-mini-realtime-preview"

    def __init__(
        self,
        instructions: str,
        voice: str = "ash",
        mic_index: int | None = None,
        on_text: Callable[[str], None] | None = None,
    ):
        self.api_key = os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY missing")

        self.voice = voice
        self.instructions = instructions
        self.on_text = on_text

        self.audio = AudioHandler(device_index=mic_index)
        self._audio_buf = b""
        self._text_buf = ""
        self._rec_flag = threading.Event()

        # Async loop in background thread
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

        # WebSocket connection
        self.ws = asyncio.run_coroutine_threadsafe(
            self._connect(), self.loop
        ).result()
        asyncio.run_coroutine_threadsafe(self._recv_loop(), self.loop)

        # GPIO (Pi only)
        self._init_gpio()

    # ---------------- GPIO ---------------- #
    def _init_gpio(self):
        if not _gpio_ok:
            return
        self.led = LED(GPIO_LED_PIN)
        self.btn = Button(GPIO_BUTTON_PIN, pull_up=True, bounce_time=0.1)
        self.btn.when_pressed = self._gpio_toggle
        log.info("GPIO control active (button %d, LED %d)", GPIO_BUTTON_PIN, GPIO_LED_PIN)

    def _gpio_toggle(self):
        if self._rec_flag.is_set():
            self.stop_talking()
            self.led.off()
        else:
            self.start_talking()
            self.led.on()

    # ---------------- WebSocket plumbing --- #
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

    async def _recv_loop(self):
        try:
            async for msg in self.ws:
                await self._handle(json.loads(msg))
        except Exception as exc:  # noqa: BLE001
            log.error("Receive loop exited: %s", exc)

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

    # ---------------- mic streaming -------- #
    async def _mic_stream(self):
        self.audio.start()
        try:
            while self._rec_flag.is_set():
                chk = self.audio.read_chunk()
                if chk:
                    await self._send(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(chk).decode(),
                        }
                    )
                await asyncio.sleep(0.0)
        finally:
            self.audio.stop()
            await self._send({"type": "input_audio_buffer.commit"})
            await self._send({"type": "response.create"})
            log.info("Audio committed")

    # ---------------- text send ------------ #
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

    # ---------------- public --------------- #
    def start_talking(self):
        if self._rec_flag.is_set():
            return
        self._rec_flag.set()
        asyncio.run_coroutine_threadsafe(self._mic_stream(), self.loop)
        log.debug("Recording started")

    def stop_talking(self):
        self._rec_flag.clear()
        log.debug("Recording stopped")

    def send_text(self, text: str):
        asyncio.run_coroutine_threadsafe(
            self._send_text_async(text), self.loop
        )

    # ---------------- cleanup -------------- #
    def close(self):
        if _gpio_ok:
            self.led.off()
        self.audio.close()
        if self.ws and not self.ws.closed:
            asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop)
        self.loop.call_soon_threadsafe(self.loop.stop())
