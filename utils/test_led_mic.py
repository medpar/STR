#!/usr/bin/env python3
"""
Test script to verify USB mic recording + LED + button wiring
without running the whole Flask app.

• Press the push‑button (GPIO17) or hit ENTER in the console to start
  recording. LED (GPIO27) lights while recording.
• Press the button again (or ENTER) to stop – WAV is written to ./recordings/.
"""

import os, time, threading, wave, logging
import numpy as np, pyaudio
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))
from config import (
    MIC_DEVICE_INDEX, SAMPLE_RATE, FRAME_CHUNK, NUM_CHANNELS,
    NORMALISE_INPUT, GPIO_BUTTON_PIN, GPIO_LED_PIN,
)

try:
    from gpiozero import Button, LED
    _gpio = True
except (ImportError, RuntimeError):
    _gpio = False
    Button = LED = None  # type: ignore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("test_mic_led")

# -------- Audio helpers ---------- #
pa = pyaudio.PyAudio()
fmt = pyaudio.paInt16

def normalise(raw:bytes)->bytes:
    if not NORMALISE_INPUT: return raw
    pcm=np.frombuffer(raw,np.int16); peak=np.max(np.abs(pcm)) or 1
    gain=int(0.9*32767/peak)
    if gain>1: pcm=np.clip(pcm*gain,-32768,32767).astype(np.int16)
    return pcm.tobytes()

def record_once():
    stream=pa.open(format=fmt,channels=NUM_CHANNELS,rate=48000,
                   input=True,frames_per_buffer=FRAME_CHUNK,
                   input_device_index=MIC_DEVICE_INDEX)
    frames=[]; recording=True
    log.info("🔴 Grabando…  (pulsar botón/ENTER para parar)")
    if _gpio: led.on()
    def wait():
        nonlocal recording
        input()
        recording=False
    threading.Thread(target=wait,daemon=True).start()
    while recording:
        frames.append(normalise(stream.read(FRAME_CHUNK,exception_on_overflow=False)))
    stream.stop_stream(); stream.close()
    if _gpio: led.off()
    ts=time.strftime("%Y%m%d-%H%M%S"); fname=f"rec_{ts}.wav"
    os.makedirs("recordings",exist_ok=True)
    with wave.open(os.path.join("recordings",fname),"wb") as wf:
        wf.setnchannels(NUM_CHANNELS)
        wf.setsampwidth(pa.get_sample_size(fmt))
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))
    log.info("✅ Guardado recordings/%s",fname)

# -------- GPIO setup ------------- #
if _gpio:
    led=LED(GPIO_LED_PIN); btn=Button(GPIO_BUTTON_PIN,pull_up=True,bounce_time=0.05)
    btn.when_pressed=lambda: threading.Thread(target=record_once,daemon=True).start()
    log.info("GPIO listo  (botón %d  LED %d)",GPIO_BUTTON_PIN,GPIO_LED_PIN)
else:
    log.info("GPIO deshabilitado – probando solo micrófono")

log.info("Pulsa botón o ENTER para empezar / parar grabación (Ctrl‑C para salir)")
try:
    while True:
        record_once()
except KeyboardInterrupt:
    pass
finally:
    pa.terminate()
    if _gpio: led.off()
