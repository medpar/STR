#!/usr/bin/env python3
"""
Realtime speech‑to‑speech for STR.

• Uses USB mic defined in config.py
• Physical push‑button (GPIO17) toggles recording
  – LED on GPIO27 lights while recording
• Web‑UI buttons still work

If you don’t have gpiozero installed or you’re running on a non‑Pi
platform, GPIO support is auto‑disabled but everything else works.
"""

from __future__ import annotations

import asyncio, base64, json, logging, os, ssl, threading
from typing import Callable, Optional

import pyaudio, websockets             # websockets==13.*
import numpy as np

from config import (
    MIC_DEVICE_INDEX, SAMPLE_RATE, FRAME_CHUNK, NUM_CHANNELS,
    NORMALISE_INPUT, GPIO_BUTTON_PIN, GPIO_LED_PIN,
)

# ------------------------------------------------------------------#
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
log = logging.getLogger("realtime")

# ------------------------------------------------------------------#
#  GPIO (automatically disabled on macOS/Win)                       #
# ------------------------------------------------------------------#
try:
    from gpiozero import Button, LED         # sudo apt install python3-gpiozero
    _gpio_enabled = True
except (ImportError, RuntimeError):
    _gpio_enabled = False
    Button = LED = None  # type: ignore


# ------------------------------------------------------------------#
#  Prompt                                                           #
# ------------------------------------------------------------------#
INSTRUCTIONS = (
    "You are a professional radio broadcaster. Provide a natural, "
    "broadcast‑style answer. Answer in Spanish from Spain."
)

# ------------------------------------------------------------------#
#  Audio handler                                                    #
# ------------------------------------------------------------------#
class AudioHandler:
    def __init__(self, device_index:int|None=None):
        self.rate, self.chunk, self.channels = SAMPLE_RATE, FRAME_CHUNK, NUM_CHANNELS
        self.fmt = pyaudio.paInt16
        self.dev = device_index
        self.pa  = pyaudio.PyAudio()
        self.stream_in = None
        self._rec = False

    # ------------- mic ------------- #
    def start(self):
        self.stream_in = self.pa.open(
            format=self.fmt, channels=self.channels, rate=self.rate,
            input=True, input_device_index=self.dev,
            frames_per_buffer=self.chunk,
        )
        self._rec = True
        log.info("Mic ON (dev=%s)", self.dev)

    def read_chunk(self)->bytes|None:
        if not self._rec: return None
        data = self.stream_in.read(self.chunk, exception_on_overflow=False)
        if NORMALISE_INPUT:
            pcm = np.frombuffer(data, np.int16)
            peak = np.max(np.abs(pcm)) or 1
            gain = int(0.9*32767/peak)
            if gain>1:
                pcm = np.clip(pcm*gain, -32768, 32767).astype(np.int16)
                data = pcm.tobytes()
        return data

    def stop(self):
        if self.stream_in:
            self.stream_in.stop_stream(); self.stream_in.close()
        self._rec=False; log.info("Mic OFF")

    def play(self, pcm:bytes):
        def _out():
            s=self.pa.open(format=self.fmt,channels=1,rate=self.rate,output=True)
            s.write(pcm); s.stop_stream(); s.close()
        threading.Thread(target=_out,daemon=True).start()

    def close(self):
        self.stop(); self.pa.terminate()

# ------------------------------------------------------------------#
#  Main Realtime client                                             #
# ------------------------------------------------------------------#
class RealtimeClient:
    _URL="wss://api.openai.com/v1/realtime"; _MODEL="gpt-4o-mini-realtime-preview"
    def __init__(self,instructions:str,voice:str="ash",
                 mic_index:int|None=None,on_text:Callable[[str],None]|None=None):
        self.api_key=os.getenv("OPENAI_API_KEY"); assert self.api_key,"OPENAI_API_KEY missing"
        self.voice, self.instructions, self.on_text = voice,instructions,on_text

        # GPIO ----------------------------------------------------- #
        self.led = self.btn = None
        if _gpio_enabled:
            self.led = LED(GPIO_LED_PIN)
            self.btn = Button(GPIO_BUTTON_PIN,pull_up=True,bounce_time=0.05)
            self.btn.when_pressed=self._gpio_toggle
            log.info("GPIO enabled (button %d, LED %d)",GPIO_BUTTON_PIN,GPIO_LED_PIN)

        # Audio & async loop --------------------------------------- #
        self.audio=AudioHandler(device_index=mic_index)
        self.loop=asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever,daemon=True).start()
        self.ws = asyncio.run_coroutine_threadsafe(self._connect(),self.loop).result()
        asyncio.run_coroutine_threadsafe(self._recv_loop(),self.loop)
        self._rec_flag=threading.Event()
        self._txt_buf,self._aud_buf=" ",b""

    # ---------- GPIO toggle ---------- #
    def _gpio_toggle(self):
        if self._rec_flag.is_set(): self.stop_talking()
        else: self.start_talking()

    # ---------- WebSocket plumbing ---- #
    async def _connect(self):
        sslctx=ssl.create_default_context(); sslctx.check_hostname=False; sslctx.verify_mode=ssl.CERT_NONE
        self.ws=await websockets.connect(f"{self._URL}?model={self._MODEL}",
            extra_headers={"Authorization":f"Bearer {self.api_key}","OpenAI-Beta":"realtime=v1"},ssl=sslctx)
        await self._send({"type":"session.update","session":{
            "modalities":["audio","text"],"instructions":self.instructions,
            "voice":self.voice,"input_audio_format":"pcm16","output_audio_format":"pcm16",
            "turn_detection":None,"input_audio_transcription":{"model":"whisper-1"},
            "temperature":0.6}})
        await self._send({"type":"response.create"}); return self.ws
    async def _send(self,ev:dict): await self.ws.send(json.dumps(ev))
    async def _recv_loop(self):
        async for m in self.ws:
            e=json.loads(m); t=e.get("type")
            if t=="response.text.delta": self._txt_buf+=e["delta"]
            elif t=="response.text.done":
                if self.on_text: self.on_text(self._txt_buf.strip()); self._txt_buf=" "
            elif t=="response.audio.delta": self._aud_buf+=base64.b64decode(e["delta"])
            elif t=="response.audio.done": self.audio.play(self._aud_buf); self._aud_buf=b""

    # ---------- mic streaming --------- #
    async def _mic_stream(self):
        self.audio.start(); self._led(True)
        try:
            while self._rec_flag.is_set():
                if (chunk:=self.audio.read_chunk()):
                    await self._send({"type":"input_audio_buffer.append",
                                      "audio":base64.b64encode(chunk).decode()})
                await asyncio.sleep(0)
        finally:
            self.audio.stop(); self._led(False)
            await self._send({"type":"input_audio_buffer.commit"})
            await self._send({"type":"response.create"})
            log.info("Audio committed")

    # ---------- external API ---------- #
    def start_talking(self):
        if self._rec_flag.is_set(): return
        self._rec_flag.set()
        asyncio.run_coroutine_threadsafe(self._mic_stream(),self.loop)
        log.debug("start_talking()")
    def stop_talking(self):
        self._rec_flag.clear(); log.debug("stop_talking()")
    def send_text(self,text:str):
        asyncio.run_coroutine_threadsafe(self._send({
            "type":"conversation.item.create","item":{"type":"message","role":"user",
            "content":[{"type":"input_text","text":text}]}}),self.loop)
        asyncio.run_coroutine_threadsafe(self._send({"type":"response.create"}),self.loop)
    def _led(self,state:bool):
        if self.led: (self.led.on if state else self.led.off)()
    def close(self):
        self._led(False); self.audio.close(); self.loop.call_soon_threadsafe(self.loop.stop())
