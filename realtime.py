#!/usr/bin/env python3
"""
Realtime speech‑to‑speech support for STR.

 * Captures microphone audio with PyAudio
 * Streams it to OpenAI’s Realtime API over Web‑Socket
 * Plays the AI voice reply locally (PCM‑16, 24 kHz)
 * Exposes `start_talking()` / `stop_talking()` so
   the Flask‑SocketIO server can control recording
   from any web client.

The module spins up its own asyncio event‑loop in a
background thread, keeping Flask (eventlet/gevent) free.
"""

import os
import ssl
import json
import base64
import asyncio
import threading
import logging
import time

import websockets                     # ==13.*  (OpenAI needs <14)
import pyaudio                        # PortAudio wrapper

# ------------------------------------------------------------------#
#  Logging                                                          #
# ------------------------------------------------------------------#
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)5s | %(name)s | %(message)s",
)
log = logging.getLogger("realtime")

# ------------------------------------------------------------------#
#  Global prompt                                                    #
# ------------------------------------------------------------------#
INSTRUCTIONS = (
    "You are a professional radio broadcaster. Provide a natural, "
    "broadcast‑style answer. Answer in castillian Spanish. Use European "
    "format for all dates and units. Do not say anything in your first "
    "message except 'Voice real time mode started.'. Answer in short, "
    "concise sentences."
)

# ------------------------------------------------------------------#
#  Audio helper                                                     #
# ------------------------------------------------------------------#
class AudioHandler:
    """Low‑level microphone capture and speaker playback"""

    def __init__(
        self,
        rate: int = 24_000,
        chunk: int = 1_024,
        channels: int = 1,
        fmt=pyaudio.paInt16,
        input_device_index: int | None = None,
    ):
        self.rate = rate
        self.chunk = chunk
        self.channels = channels
        self.fmt = fmt
        self.p = pyaudio.PyAudio()
        self.stream_in = None
        self.input_device_index = input_device_index
        self._recording = False

        # Display detected capture devices (handy for Pi vs. Mac)
        for i in range(self.p.get_device_count()):
            dev = self.p.get_device_info_by_index(i)
            if dev["maxInputChannels"]:
                log.debug("Input‑ID %s – %s", i, dev["name"])

    # ----------  microphone  ----------#
    def start_recording(self) -> None:
        if self.stream_in:
            self.stop_recording()

        self.stream_in = self.p.open(
            format=self.fmt,
            channels=self.channels,
            rate=self.rate,
            input=True,
            frames_per_buffer=self.chunk,
            input_device_index=self.input_device_index,
        )
        self._recording = True
        log.info("Microphone stream opened (device=%s)", self.input_device_index)

    def read_chunk(self) -> bytes | None:
        if not self._recording or not self.stream_in:
            return None
        try:
            return self.stream_in.read(
                self.chunk, exception_on_overflow=False
            )
        except Exception as exc:  # noqa: BLE001
            log.error("Error reading mic chunk: %s", exc)
            return None

    def stop_recording(self) -> None:
        if self.stream_in:
            self.stream_in.stop_stream()
            self.stream_in.close()
            self.stream_in = None
        self._recording = False
        log.info("Microphone stream closed")

    # ----------  speaker  ----------#
    def play_audio(self, pcm16_audio: bytes) -> None:
        def _play():
            try:
                stream_out = self.p.open(
                    format=self.fmt,
                    channels=1,
                    rate=self.rate,
                    output=True,
                )
                stream_out.write(pcm16_audio)
                stream_out.stop_stream()
                stream_out.close()
            except Exception as exc:  # noqa: BLE001
                log.error("Speaker playback error: %s", exc)

        threading.Thread(target=_play, daemon=True).start()

    # ----------  cleanup  ----------#
    def close(self) -> None:
        self.stop_recording()
        self.p.terminate()


# ------------------------------------------------------------------#
#  RealtimeClient                                                   #
# ------------------------------------------------------------------#
class RealtimeClient:
    """
    One shared instance is created in *app.py* and lives
    for the entire Flask lifetime.

    Public methods:
        * start_talking()   – begin mic capture / streaming
        * stop_talking()    – commit buffer, wait for reply
    """

    _URL = "wss://api.openai.com/v1/realtime"
    _MODEL = "gpt-4o-mini-realtime-preview"

    def __init__(
        self,
        instructions: str,
        voice: str = "ash",
        on_text=None,
        mic_index: int | None = None,
    ):
        self.api_key = os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY not set in environment")

        self.voice = voice
        self.instructions = instructions
        self.on_text = on_text  # callback(str) -> None

        # Audio
        self.audio = AudioHandler(input_device_index=mic_index)
        self._audio_buffer = b""
        self._text_buffer = ""

        # Async infrastructure
        self.loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self.loop.run_forever, daemon=True
        )
        self._loop_thread.start()

        self.ws = asyncio.run_coroutine_threadsafe(
            self._connect(), self.loop
        ).result()

        # Start listener coroutine
        asyncio.run_coroutine_threadsafe(
            self._receive_events(), self.loop
        )

        # Runtime flags
        self._recording_flag = threading.Event()

    # ---------------- private asyncio parts ----------------#
    async def _connect(self):
        log.info("Connecting to OpenAI Realtime API …")
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

        session_cfg = {
            "modalities": ["audio", "text"],
            "instructions": self.instructions,
            "voice": self.voice,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "turn_detection": None,            # manual VAD
            "input_audio_transcription": {"model": "whisper-1"},
            "temperature": 0.6,
        }

        await self._send({
            "type": "session.update",
            "session": session_cfg,
        })
        await self._send({"type": "response.create"})
        log.info("OpenAI session configured")
        return self.ws

    async def _send(self, event: dict) -> None:
        await self.ws.send(json.dumps(event))

    async def _receive_events(self) -> None:
        try:
            async for message in self.ws:
                await self._handle_event(json.loads(message))
        except Exception as exc:  # noqa: BLE001
            log.error("WebSocket receive loop exited: %s", exc)

    async def _handle_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "error":
            log.error("API error: %s", event["error"]["message"])

        elif etype == "response.text.delta":
            self._text_buffer += event["delta"]

        elif etype == "response.text.done":
            if self.on_text:
                self.on_text(self._text_buffer)
            self._text_buffer = ""

        elif etype == "response.audio.delta":
            self._audio_buffer += base64.b64decode(event["delta"])

        elif etype == "response.audio.done":
            if self._audio_buffer:
                self.audio.play_audio(self._audio_buffer)
            self._audio_buffer = b""

        # Other event types are logged for debugging
        else:
            log.debug("Event: %s", etype)

    # ---------------- microphone streaming ----------------#
    async def _stream_mic_until_stop(self):
        self.audio.start_recording()
        log.info("✨  Mic streaming started")
        try:
            while self._recording_flag.is_set():
                chunk = self.audio.read_chunk()
                if not chunk:
                    # allow a tiny sleep to avoid tight loop
                    await asyncio.sleep(0.01)
                    continue
                await self._send(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(chunk).decode("utf‑8"),
                    }
                )
                await asyncio.sleep(0.0)  # let loop breathe
        finally:
            self.audio.stop_recording()
            # Commit & request model response
            await self._send({"type": "input_audio_buffer.commit"})
            await self._send({"type": "response.create"})
            log.info("🎤  Mic streaming stopped – waiting for reply")

    # ---------------- public helpers (thread‑safe) ---------#
    def start_talking(self):
        if self._recording_flag.is_set():
            return  # already recording
        self._recording_flag.set()
        asyncio.run_coroutine_threadsafe(
            self._stream_mic_until_stop(), self.loop
        )
        log.debug("start_talking() dispatched")

    def stop_talking(self):
        self._recording_flag.clear()
        log.debug("stop_talking() dispatched")

    # ---------------- graceful shutdown --------------------#
    def close(self):
        self.audio.close()
        if self.ws and not self.ws.closed:
            asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop)
        self.loop.call_soon_threadsafe(self.loop.stop())
        self._loop_thread.join()


# Convenience factory for external import
def build_realtime_client(on_text=None) -> RealtimeClient:
    mic_idx = (
        int(os.getenv("MIC_DEVICE_INDEX"))
        if os.getenv("MIC_DEVICE_INDEX")
        else None
    )
    return RealtimeClient(
        instructions=INSTRUCTIONS,
        voice="alloy",
        on_text=on_text,
        mic_index=mic_idx,
    )
