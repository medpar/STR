#!/usr/bin/env python3
"""
Standalone tester for USB mic + external‑resistor button + LED.

* Push‑button toggles recording (LED ON while capturing).
* WAV saved in ./recordings/ using the mic’s *native* sample‑rate so
  playback speed is always correct.

Run on the Pi:

    pip install numpy RPi.GPIO pyaudio
    python utils/test_mic_led.py
"""

from __future__ import annotations
import os, time, wave, threading, sys, signal
from pathlib import Path
import numpy as np
import pyaudio
import RPi.GPIO as GPIO  # Use low‑level library – no internal pulls

# -- import config from parent dir ----------------------------------#
sys.path.append(str(Path(__file__).resolve().parents[1]))
from config import (          # type: ignore
    MIC_DEVICE_INDEX,
    MIC_SAMPLE_RATE,
    MIC_CHANNELS,
    MIC_CHUNK,
    MIC_NORMALISE,
    GPIO_BUTTON_PIN,
    GPIO_LED_PIN,
    BUTTON_ACTIVE_HIGH,
)

# ---------------- GPIO setup --------------------------------------#
GPIO.setmode(GPIO.BCM)
GPIO.setup(GPIO_LED_PIN, GPIO.OUT, initial=GPIO.LOW)

PULL = GPIO.PUD_DOWN if BUTTON_ACTIVE_HIGH else GPIO.PUD_UP
GPIO.setup(GPIO_BUTTON_PIN, GPIO.IN, pull_up_down=PULL)

# ---------------- audio globals -----------------------------------#
pa = pyaudio.PyAudio()
stream = None
frames: list[bytes] = []
recording = False
lock = threading.Lock()

# Query the mic’s native sample‑rate if not fixed in config
if MIC_SAMPLE_RATE == 0:
    dev_info = pa.get_device_info_by_index(MIC_DEVICE_INDEX)
    SAMPLE_RATE = int(dev_info["defaultSampleRate"])
else:
    SAMPLE_RATE = MIC_SAMPLE_RATE

print(f"📌 Mic idx={MIC_DEVICE_INDEX}, native rate={SAMPLE_RATE} Hz")
print(f"📌 Button GPIO{GPIO_BUTTON_PIN} (active={'HIGH' if BUTTON_ACTIVE_HIGH else 'LOW'})")
print(f"📌 LED    GPIO{GPIO_LED_PIN}")

# ---------------- helpers -----------------------------------------#
def start_recording():
    global stream, frames, recording
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=MIC_CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=MIC_CHUNK,
        input_device_index=MIC_DEVICE_INDEX,
    )
    frames = []
    recording = True
    GPIO.output(GPIO_LED_PIN, GPIO.HIGH)
    print("🎙️  REC")

def stop_recording():
    global stream, recording
    if stream:
        stream.stop_stream()
        stream.close()
        stream = None
    recording = False
    GPIO.output(GPIO_LED_PIN, GPIO.LOW)
    print("⏹️  STOP")

def save_wav():
    if not frames:
        print("⚠️  no audio captured")
        return
    out_dir = Path("recordings")
    out_dir.mkdir(exist_ok=True)
    fname = out_dir / f"mic_{time.strftime('%Y%m%d-%H%M%S')}.wav"
    with wave.open(str(fname), "wb") as wf:
        wf.setnchannels(MIC_CHANNELS)
        wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))
    print(f"✅ saved {fname.resolve()}")

def capture_loop():
    while True:
        if recording and stream:
            data = stream.read(MIC_CHUNK, exception_on_overflow=False)
            if MIC_NORMALISE:
                audio = np.frombuffer(data, np.int16)
                peak = np.max(np.abs(audio)) or 1
                gain = int(0.9 * 32767 / peak)
                if gain > 1:
                    audio = np.clip(audio * gain, -32768, 32767).astype(np.int16)
                    data = audio.tobytes()
            frames.append(data)
        else:
            time.sleep(0.01)

def button_callback(channel):
    with lock:
        if recording:
            stop_recording()
            save_wav()
        else:
            start_recording()

GPIO.add_event_detect(
    GPIO_BUTTON_PIN,
    GPIO.RISING if BUTTON_ACTIVE_HIGH else GPIO.FALLING,
    callback=button_callback,
    bouncetime=150,
)

threading.Thread(target=capture_loop, daemon=True).start()

# ---------------- graceful exit -----------------------------------#
def cleanup(sig, frame):
    if recording:
        stop_recording()
        save_wav()
    pa.terminate()
    GPIO.cleanup()
    print("\n👋 bye")
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup)
print("Press the push‑button to start/stop recording – Ctrl‑C exits.")
signal.pause()
