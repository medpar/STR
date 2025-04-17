#!/usr/bin/env python3
"""
Press the button on GPIO17 to start/stop recording.
The LED on GPIO27 is lit while recording.
A WAV is written to ./recordings/rec_YYYYMMDD‑HHMMSS.wav

Playback speed is now correct (48 kHz by default, change in config.py).
"""

import os, time, wave, threading
import numpy as np, pyaudio, RPi.GPIO as GPIO         # pip install numpy

from config import SAMPLE_RATE, NUM_CHANNELS, FRAME_CHUNK, \
                   MIC_DEVICE_INDEX, GPIO_BUTTON_PIN, GPIO_LED_PIN

GPIO.setmode(GPIO.BCM)
GPIO.setup(GPIO_BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(GPIO_LED_PIN,    GPIO.OUT, initial=GPIO.LOW)

pa = pyaudio.PyAudio()
stream = None
frames = []
recording = False
lock = threading.Lock()

def toggle(channel):
    global stream, frames, recording
    with lock:
        if not recording:
            # --- start ---
            stream = pa.open(format=pyaudio.paInt16, channels=NUM_CHANNELS,
                             rate=SAMPLE_RATE, input=True,
                             frames_per_buffer=FRAME_CHUNK,
                             input_device_index=MIC_DEVICE_INDEX)
            frames = []
            recording = True
            GPIO.output(GPIO_LED_PIN, GPIO.HIGH)
            print("🔴 REC…  (press again to stop)")
            threading.Thread(target=capture_loop, daemon=True).start()
        else:
            # --- stop ---
            recording = False
            GPIO.output(GPIO_LED_PIN, GPIO.LOW)
            stream.stop_stream(); stream.close()
            ts = time.strftime("%Y%m%d-%H%M%S")
            path = f"recordings/rec_{ts}.wav"
            save_wav(path, b"".join(frames))
            print(f"✅ saved {path}")

def capture_loop():
    while recording:
        try:
            data = stream.read(FRAME_CHUNK, exception_on_overflow=False)
            frames.append(data)
        except OSError as e:
            print("overflow", e)

def save_wav(path, raw):
    os.makedirs("recordings", exist_ok=True)
    audio = np.frombuffer(raw, np.int16)
    if audio.size:
        peak = np.max(np.abs(audio)); gain = int(0.9*32767/peak) if peak else 1
        if gain > 1: audio = np.clip(audio*gain, -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(NUM_CHANNELS)
        wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())

GPIO.add_event_detect(GPIO_BUTTON_PIN, GPIO.FALLING, callback=toggle, bouncetime=200)
print("▶️  Ready. Press the button to record. Ctrl‑C to exit.")

try:
    while True: time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    if stream: stream.close()
    pa.terminate()
    GPIO.cleanup()
