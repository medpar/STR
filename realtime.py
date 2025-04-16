#!/usr/bin/env python3
"""
Realtime speech‑to‑speech support for STR.

• Capture microphone audio with PyAudio
• Send mic or user‑typed text to OpenAI Realtime API
• Play voice replies (PCM‑16, 24 kHz)
• Broadcast every assistant text delta to Flask‑SocketIO
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
import websockets                        # ==13.* required (<14)

# ------------------------------------------------------------------#
#  Logging                                                          #
# ------------------------------------------------------------------#
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)5s | %(name)s | %(message)s",
)
log = logging.getLogger("realtime")

# ------------------------------------------------------------------#
#  System‑wide prompt                                               #
# ------------------------------------------------------------------#
INSTRUCTIONS = (
    "You are a professional radio broadcaster. Provide a natural, "
    "broadcast‑style answer. Answer in Spanish from Spain. Use European "
    "format for all dates and units. Do not say anything in your first "
    "message except 'Voice real time mode started.'. Answer in short, "
    "concise sentences."
)

# ------------------------------------------------------------------#
#  Audio helper                                                     #
# ------------------------------------------------------------------#
class AudioHandler:
    """Microphone capture & speaker playback (PyAudio)."""

    def __init__(
        self,
        rate: int = 24_000,
        chunk: int = 1_024,
        channels: int = 1,
        fmt: int = pyaudio.paInt16,
        input_device_index: Optional[int] = None,
    ):
        self.rate = rate
        self.chunk = chunk
        self.channels = channels
        self.fmt = fmt
        self.input_device_index = input_device_index
        self.p = pyaudio.PyAudio()
        self.stream_in = None
        self._recording = False

        # Show available capture devices (handy on Pi vs Mac)
        for i in range(self.p.get_device_count()):
            dev = self.p.get_device_info_by_index(i)
            if dev["maxInputChannels"]:
                log.debug("Input‑ID %s – %s", i, dev["name"])

    # ------------- microphone -------------#
    def start_rec(self) -> None:
        if self.stream_in:
            self.stop_rec()
        self.stream_in = self.p.open(
            format=self.fmt,
            channels=self.channels,
            rate=self.rate,
            input=True,
            frames_per_buffer=self.chunk,
            input_device_index=self.input_device_index,
        )
        self._recording = True
        log.info("🎙️  Mic ON (%s)", self.input_device_index)

    def read_chunk(self) -> bytes | None:
        if not self._recording or not self.stream_in:
            return None
        try:
            return self.stream_in.read(
                self.chunk, exception_on_overflow=False
            )
        except Exception as exc:  # noqa: BLE001
            log.error("Mic chunk error: %s", exc)
            return None

    def stop_rec(self) -> None:
        if self.stream_in:
            self.stream_in.stop_stream()
            self.stream_in.close()
            self.stream_in = None
        self._recording = False
        log.info("🎙️  Mic OFF")

    # ------------- speaker ---------------#
    def play(self, pcm16_audio: bytes) -> None:
        def _worker():
            try:
                out = self.p.open(
                    format=self.fmt, channels=1, rate=self.rate, output=True
                )
                out.write(pcm16_audio)
                out.stop_stream()
                out.close()
            except Exception as exc:  # noqa: BLE001
                log.error("Speaker error: %s", exc)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------- cleanup ---------------#
    def close(self) -> None:
        self.stop_rec()
        self.p.terminate()


# ------------------------------------------------------------------#
#  RealtimeClient                                                   #
# ------------------------------------------------------------------#
class RealtimeClient:
    """
    One shared instance lives inside app.py.

    Public methods (thread‑safe):
        • start_talking()
        • stop_talking()
        • send_text(str)
    """

    _URL = "wss://api.openai.com/v1/realtime"
    _MODEL = "gpt-4o-mini-realtime-preview"

    def __init__(
        self,
        instructions: str,
        voice: str = "ash",                       # <<<  voice changed here
        mic_index: Optional[int] = None,
        on_text: Callable[[str], None] | None = None,
    ):
        # -- keys & loop ----------------------------------------------------#
        self.api_key = os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY not set")

        self.voice = voice
        self.instructions = instructions
        self.on_text = on_text

        self.audio = AudioHandler(input_device_index=mic_index)
        self._audio_buf = b""
        self._txt_buf = ""

        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

        self.ws = asyncio.run_coroutine_threadsafe(
            self._connect(), self.loop
        ).result()
        asyncio.run_coroutine_threadsafe(
            self._receive_loop(), self.loop
        )

        self._rec_flag = threading.Event()

    # ---------------- WebSocket plumbing ------------------#
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
                    "turn_detection": None,  # manual VAD
                    "input_audio_transcription": {"model": "whisper-1"},
                    "temperature": 0.6,
                },
            }
        )
        await self._send({"type": "response.create"})
        log.info("OpenAI Realtime session ready")
        return self.ws

    async def _send(self, ev: dict) -> None:
        await self.ws.send(json.dumps(ev))

    async def _receive_loop(self):
        try:
            async for msg in self.ws:
                await self._handle(json.loads(msg))
        except Exception as exc:  # noqa: BLE001
            log.error("Receive loop exited: %s", exc)

    async def _handle(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "error":
            log.error("API error: %s", ev["error"]["message"])
        elif t == "response.text.delta":
            self._txt_buf += ev["delta"]
        elif t == "response.text.done":
            if self.on_text:
                self.on_text(self._txt_buf)
            self._txt_buf = ""
        elif t == "response.audio.delta":
            self._audio_buf += base64.b64decode(ev["delta"])
        elif t == "response.audio.done":
            if self._audio_buf:
                self.audio.play(self._audio_buf)
            self._audio_buf = b""
        else:
            log.debug("Event %s", t)

    # ---------------- mic streaming -----------------------#
    async def _mic_stream(self):
        self.audio.start_rec()
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
            self.audio.stop_rec()
            await self._send({"type": "input_audio_buffer.commit"})
            await self._send({"type": "response.create"})
            log.info("Mic buffer committed")

    # ---------------- text send ---------------------------#
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

    # ---------------- public (thread‑safe) ----------------#
    def start_talking(self):
        if self._rec_flag.is_set():
            return
        self._rec_flag.set()
        asyncio.run_coroutine_threadsafe(self._mic_stream(), self.loop)

    def stop_talking(self):
        self._rec_flag.clear()

    def send_text(self, text: str):
        asyncio.run_coroutine_threadsafe(
            self._send_text_async(text), self.loop
        )

    # ---------------- shutdown ----------------------------#
    def close(self):
        self.audio.close()
        if self.ws and not self.ws.closed:
            asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop)
        self.loop.call_soon_threadsafe(self.loop.stop())
