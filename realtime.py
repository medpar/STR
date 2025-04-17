#!/usr/bin/env python3
"""
Realtime speech‑to‑speech with reliable GPIO control.

Fixes:
• Cleans up any previous GPIO registrations before (re)initialising.
• Disables GPIO warnings (“channel already in use”).
• Uses edge‑callback instead of polling thread → no failed
  add_event_detect, LED always follows recording state.
"""

from __future__ import annotations
import asyncio, base64, json, logging, os, ssl, threading

import pyaudio, websockets, numpy as np          # pip install websockets==13.* numpy
from typing import Callable, Optional
from config import (
    MIC_DEVICE_INDEX, SAMPLE_RATE, NUM_CHANNELS, FRAME_CHUNK, NORMALISE_INPUT,
    GPIO_BUTTON_PIN, GPIO_LED_PIN,
)

# ---------- GPIO ---------- #
try:
    import RPi.GPIO as GPIO
    GPIO.setwarnings(False)
    GPIO.cleanup()                               # clear leftovers from any crashed script
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(GPIO_BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(GPIO_LED_PIN,    GPIO.OUT, initial=GPIO.LOW)
    _gpio_ok = True
except (ImportError, RuntimeError):
    _gpio_ok = False
    GPIO = None                                  # type: ignore

# ---------- logging ---------- #
logging.basicConfig(level=logging.INFO, format="%(asctime)s | realtime | %(message)s")
log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "You are a professional radio broadcaster. Provide a natural, broadcast‑style answer. "
    "Answer in Spanish from Spain. Use European format. Say only "
    "'Voice real time mode started.' as the first message."
)

# ------------------------------------------------------------------#
#  AudioHandler                                                     #
# ------------------------------------------------------------------#
class AudioHandler:
    def __init__(self, dev_index: int | None):
        self.rate, self.chunk, self.channels = SAMPLE_RATE, FRAME_CHUNK, NUM_CHANNELS
        self.dev_index = dev_index
        self.pa, self.stream, self.recording = pyaudio.PyAudio(), None, False

    def start(self):
        self.stream = self.pa.open(format=pyaudio.paInt16, channels=self.channels,
                                   rate=self.rate, input=True, frames_per_buffer=self.chunk,
                                   input_device_index=self.dev_index)
        self.recording = True
        log.info("Mic ON")

    def read(self) -> bytes | None:
        if not self.recording: return None
        raw = self.stream.read(self.chunk, exception_on_overflow=False)
        if NORMALISE_INPUT:
            arr = np.frombuffer(raw, np.int16)
            if arr.size and (pk := np.max(np.abs(arr))):
                g = int(0.9*32767/pk)
                if g > 1: raw = np.clip(arr*g, -32768, 32767).astype(np.int16).tobytes()
        return raw

    def stop(self):
        if self.stream:
            self.stream.stop_stream(); self.stream.close(); self.stream = None
        self.recording = False
        log.info("Mic OFF")

    def play(self, pcm16: bytes):
        def _th():
            out = self.pa.open(format=pyaudio.paInt16, channels=1, rate=self.rate, output=True)
            out.write(pcm16); out.stop_stream(); out.close()
        threading.Thread(target=_th, daemon=True).start()

    def close(self):
        self.stop(); self.pa.terminate()

# ------------------------------------------------------------------#
#  RealtimeClient                                                   #
# ------------------------------------------------------------------#
class RealtimeClient:
    _URL, _MODEL = "wss://api.openai.com/v1/realtime", "gpt-4o-mini-realtime-preview"

    def __init__(self, instructions: str, voice="ash", mic_index=None,
                 on_text: Callable[[str],None]|None=None):
        self.key = os.getenv("OPENAI_API_KEY") or ""
        self.voice, self.instructions, self.on_text = voice, instructions, on_text
        self.audio, self.flag = AudioHandler(mic_index), threading.Event()

        # event‑loop in background
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.ws = asyncio.run_coroutine_threadsafe(self._connect(), self.loop).result()
        asyncio.run_coroutine_threadsafe(self._recv(), self.loop)

        # GPIO callback
        if _gpio_ok:
            try: GPIO.remove_event_detect(GPIO_BUTTON_PIN)
            except RuntimeError: pass
            GPIO.add_event_detect(GPIO_BUTTON_PIN, GPIO.FALLING,
                                  callback=lambda ch: self.toggle(), bouncetime=200)

    # ---------------- WebSocket ---------------- #
    async def _connect(self):
        sslc = ssl.create_default_context()
        self.ws = await websockets.connect(
            f"{self._URL}?model={self._MODEL}",
            extra_headers={"Authorization": f"Bearer {self.key}", "OpenAI-Beta": "realtime=v1"},
            ssl=sslc,
        )
        await self._send({"type":"session.update","session":{
            "modalities":["audio","text"],"instructions":self.instructions,
            "voice":self.voice,"input_audio_format":"pcm16","output_audio_format":"pcm16"}}
        )
        await self._send({"type":"response.create"})
        log.info("Realtime session ready"); return self.ws

    async def _send(self, ev): await self.ws.send(json.dumps(ev))

    async def _recv(self):
        bufA, bufT = b"", ""
        try:
            async for m in self.ws:
                ev = json.loads(m); t=ev["type"]
                if t=="response.text.delta": bufT+=ev["delta"]
                elif t=="response.text.done": self.on_text and self.on_text(bufT); bufT=""
                elif t=="response.audio.delta": bufA+=base64.b64decode(ev["delta"])
                elif t=="response.audio.done": self.audio.play(bufA); bufA=b""
        except Exception as e: log.error("recv ended %s", e)

    # ---------------- mic streaming ------------- #
    async def _stream(self):
        self.audio.start(); self._led(True)
        while self.flag.is_set():
            if (d:=self.audio.read()): await self._send(
                {"type":"input_audio_buffer.append",
                 "audio":base64.b64encode(d).decode()})
        self.audio.stop(); self._led(False)
        await self._send({"type":"input_audio_buffer.commit"})
        await self._send({"type":"response.create"})

    # ---------------- helpers ------------------- #
    def _led(self, on: bool):
        if _gpio_ok: GPIO.output(GPIO_LED_PIN, GPIO.HIGH if on else GPIO.LOW)

    def toggle(self):
        if self.flag.is_set():
            self.flag.clear()
        else:
            self.flag.set()
            asyncio.run_coroutine_threadsafe(self._stream(), self.loop)

    # Public API used by web UI
    start_talking = toggle
    stop_talking  = toggle
    def send_text(self, txt:str):
        asyncio.run_coroutine_threadsafe(
            self._send({"type":"conversation.item.create",
                        "item":{"type":"message","role":"user",
                                "content":[{"type":"input_text","text":txt}]}}), self.loop)
        asyncio.run_coroutine_threadsafe(self._send({"type":"response.create"}), self.loop)

    def close(self):
        self.audio.close()
        if _gpio_ok: GPIO.cleanup()
