#!/usr/bin/env python3
"""
Standalone tester for USB mic + external‑resistor button + LED,
using polling to detect presses.

• Button press (GPIO17 HIGH) → LED on, start recording.
• Next press → LED off, stop & save WAV.
• Ctrl‑C exits (saves any in‑progress recording).

Run on the Pi:
    pip install numpy RPi.GPIO pyaudio
    python utils/test_mic_led.py
"""

import os
import sys
import time
import wave
import threading
import signal
from pathlib import Path

import numpy as np
import pyaudio
import RPi.GPIO as GPIO

# Import config from parent dir
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
# External pull‑down: we provide no internal resistor
GPIO.setup(GPIO_BUTTON_PIN, GPIO.IN)  
GPIO.setup(GPIO_LED_PIN, GPIO.OUT, initial=GPIO.LOW)

# ---------------- audio globals -----------------------------------#
pa = pyaudio.PyAudio()
stream = None
frames = []
recording = False
lock = threading.Lock()

# Determine sample rate
if MIC_SAMPLE_RATE == 0:
    info = pa.get_device_info_by_index(MIC_DEVICE_INDEX)
    SAMPLE_RATE = int(info["defaultSampleRate"])
else:
    SAMPLE_RATE = MIC_SAMPLE_RATE

SAMPLE_RATE = 48000  # Force to 48kHz for now

print(f"📌 Mic idx={MIC_DEVICE_INDEX}, rate={SAMPLE_RATE} Hz")
print(f"📌 Button GPIO{GPIO_BUTTON_PIN}, LED GPIO{GPIO_LED_PIN}")
print("📌 Wiring: Button to 3.3V; 10 kΩ resistor GPIO→GND (pull‑down).")

# ---------------- recording helpers --------------------------------#
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
    print("🎙️  REC started")

def stop_recording():
    global stream, recording
    if stream:
        stream.stop_stream()
        stream.close()
        stream = None
    recording = False
    GPIO.output(GPIO_LED_PIN, GPIO.LOW)
    print("⏹️  REC stopped")

def save_wav():
    if not frames:
        print("⚠️  No audio captured")
        return
    out_dir = Path("recordings")
    out_dir.mkdir(exist_ok=True)
    fname = out_dir / f"mic_{time.strftime('%Y%m%d-%H%M%S')}.wav"
    with wave.open(str(fname), "wb") as wf:
        wf.setnchannels(MIC_CHANNELS)
        wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))
    print(f"✅ Saved → {fname.resolve()}")

def capture_loop():
    """Continuously read audio while `recording` is True."""
    global frames
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

# ---------------- button polling -----------------------------------#
def poll_button():
    """Poll the button state and trigger on rising edges."""
    last_state = GPIO.input(GPIO_BUTTON_PIN)
    while True:
        state = GPIO.input(GPIO_BUTTON_PIN)
        # Detect transition from LOW→HIGH
        if not last_state and state:
            with lock:
                if recording:
                    stop_recording()
                    save_wav()
                else:
                    start_recording()
        last_state = state
        time.sleep(0.05)  # debounce interval

# ---------------- graceful exit ------------------------------------#
def cleanup(sig, frame):
    print("\n👋 Exiting…")
    if recording:
        stop_recording()
        save_wav()
    pa.terminate()
    GPIO.output(GPIO_LED_PIN, GPIO.LOW)
    GPIO.cleanup()
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup)

# ---------------- main ---------------------------------------------#
if __name__ == "__main__":
    print("Press hardware button to toggle recording. Ctrl‑C to quit.")
    threading.Thread(target=capture_loop, daemon=True).start()
    threading.Thread(target=poll_button, daemon=True).start()
    signal.pause()
