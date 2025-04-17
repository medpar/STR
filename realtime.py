#!/usr/bin/env python3
"""
Realtime speech‑to‑speech for STR with physical GPIO control.

Fixes:
• Uses 48 kHz (or whatever you set in config.py) so playback speed is correct.
• Switched to RPi.GPIO for reliable edge detection & LED output.
• Debounced polling thread toggles recording + LED and logs every step.
"""

from __future__ import annotations

import asyncio, base64, json, logging, os, ssl, threading, time
from typing import Callable, Optional

import pyaudio, websockets, numpy as np          # pip install websockets==13.* numpy
from config import (
    MIC_DEVICE_INDEX, SAMPLE_RATE, NUM_CHANNELS, FRAME_CHUNK, NORMALISE_INPUT,
    GPIO_BUTTON_PIN, GPIO_LED_PIN,
)

# -------- GPIO (only on Pi) -------- #
try:
    import RPi.GPIO as GPIO                    # sudo apt‑get install python3‑rpi.gpio
    _gpio_ok = True
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(GPIO_BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)  # pull‑up
    GPIO.setup(GPIO_LED_PIN,    GPIO.OUT, initial=GPIO.LOW)
except (ImportError, RuntimeError):
    _gpio_ok = False
    GPIO = None                                # type: ignore

# -------- logging -------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | realtime | %(message)s",
)
log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "You are a professional radio broadcaster. Provide a natural, "
    "broadcast‑style answer. Answer in Spanish from Spain. Use European "
    "format. Say only 'Voice real time mode started.' as first message."
)

# ------------------------------------------------------------------#
#  AudioHandler                                                     #
# ------------------------------------------------------------------#
class AudioHandler:
    """USB mic capture + speaker playback, per‑chunk normalisation."""

    def __init__(self, dev_index: int | None):
        self.rate, self.chunk, self.channels = SAMPLE_RATE, FRAME_CHUNK, NUM_CHANNELS
        self.dev_index = dev_index
        self.pa = pyaudio.PyAudio()
        self.stream = None
        self._rec = False

    def start(self):
        if self.stream:
            self.stop()
        self.stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.rate,
            input=True,
            frames_per_buffer=self.chunk,
            input_device_index=self.dev_index,
        )
        self._rec = True
        log.info("Mic ON  (device %s, %d Hz)", self.dev_index, self.rate)

    def read(self) -> bytes | None:
        if not self._rec:
            return None
        raw = self.stream.read(self.chunk, exception_on_overflow=False)
        if NORMALISE_INPUT:
            arr = np.frombuffer(raw, np.int16)
            if arr.size:
                peak = np.max(np.abs(arr))
                if peak:
                    gain = int(0.9 * 32767 / peak)
                    if gain > 1:
                        arr = np.clip(arr * gain, -32768, 32767).astype(np.int16)
                        raw = arr.tobytes()
        return raw

    def stop(self):
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None
        self._rec = False
        log.info("Mic OFF")

    # play helper
    def play(self, pcm16: bytes):
        def _th():
            out = self.pa.open(format=pyaudio.paInt16, channels=1, rate=self.rate, output=True)
            out.write(pcm16); out.stop_stream(); out.close()
        threading.Thread(target=_th, daemon=True).start()

    def close(self):
        self.stop()
        self.pa.terminate()

# ------------------------------------------------------------------#
#  RealtimeClient                                                   #
# ------------------------------------------------------------------#
class RealtimeClient:
    _URL = "wss://api.openai.com/v1/realtime"
    _MODEL = "gpt-4o-mini-realtime-preview"

    def __init__(self, instr: str, voice="ash", mic_index=None, on_text: Callable[[str],None]|None=None):
        self.key = os.getenv("OPENAI_API_KEY") or ""
        self.voice, self.instr, self.on_text = voice, instr, on_text
        self.audio = AudioHandler(mic_index)
        self.loop, self._flag = asyncio.new_event_loop(), threading.Event()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

        self.ws = asyncio.run_coroutine_threadsafe(self._connect(), self.loop).result()
        asyncio.run_coroutine_threadsafe(self._recv(), self.loop)

        if _gpio_ok:
            threading.Thread(target=self._gpio_poll, daemon=True).start()

    # ---------------- GPIO poll ---------------- #
    def _gpio_poll(self):
        prev = GPIO.input(GPIO_BUTTON_PIN)
        while True:
            cur = GPIO.input(GPIO_BUTTON_PIN)
            if prev == GPIO.HIGH and cur == GPIO.LOW:      # button press
                self.toggle()
            prev = cur
            time.sleep(0.05)

    def _led(self, on: bool):
        if _gpio_ok:
            GPIO.output(GPIO_LED_PIN, GPIO.HIGH if on else GPIO.LOW)

    # ---------------- ws plumbing --------------- #
    async def _connect(self):
        ssl_ctx = ssl.create_default_context()
        self.ws = await websockets.connect(
            f"{self._URL}?model={self._MODEL}",
            extra_headers={"Authorization": f"Bearer {self.key}", "OpenAI-Beta": "realtime=v1"},
            ssl=ssl_ctx,
        )
        await self._send({
            "type": "session.update",
            "session": {
                "modalities": ["audio", "text"],
                "instructions": self.instr,
                "voice": self.voice,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "turn_detection": None,
                "input_audio_transcription": {"model": "whisper-1"},
            },
        })
        await self._send({"type": "response.create"})
        log.info("Realtime session ready")
        return self.ws

    async def _send(self, ev): await self.ws.send(json.dumps(ev))

    async def _recv(self):
        buf_a, buf_t = b"", ""
        try:
            async for m in self.ws:
                ev = json.loads(m)
                t = ev["type"]
                if t == "response.text.delta":
                    buf_t += ev["delta"]
                elif t == "response.text.done":
                    if self.on_text: self.on_text(buf_t)
                    buf_t = ""
                elif t == "response.audio.delta":
                    buf_a += base64.b64decode(ev["delta"])
                elif t == "response.audio.done":
                    if buf_a: self.audio.play(buf_a)
                    buf_a = b""
        except Exception as e:
            log.error("recv loop ended: %s", e)

    # ---------------- mic streaming ------------- #
    async def _stream(self):
        self.audio.start(); self._led(True)
        try:
            while self._flag.is_set():
                if data := self.audio.read():
                    await self._send({"type": "input_audio_buffer.append",
                                      "audio": base64.b64encode(data).decode()})
        finally:
            self.audio.stop(); self._led(False)
            await self._send({"type": "input_audio_buffer.commit"})
            await self._send({"type": "response.create"})
            log.info("audio committed")

    # ---------------- public API ---------------- #
    def toggle(self):
        if self._flag.is_set():
            self._flag.clear()
        else:
            self._flag.set()
            asyncio.run_coroutine_threadsafe(self._stream(), self.loop)

    def start_talking(self):  self.toggle()   # web UI uses these
    def stop_talking(self):   self.toggle()
    def send_text(self, txt: str):
        asyncio.run_coroutine_threadsafe(
            self._send({"type":"conversation.item.create",
                        "item":{"type":"message","role":"user",
                                "content":[{"type":"input_text","text":txt}]}}), self.loop)
        asyncio.run_coroutine_threadsafe(self._send({"type":"response.create"}), self.loop)

    def close(self):
        self.audio.close()
        if _gpio_ok: GPIO.cleanup()
