#!/usr/bin/env python3
"""
Press the button on GPIO17 to start/stop recording.
LED on GPIO27 lights while recording.
WAV saved to ./recordings/rec_YYYYMMDD‑HHMMSS.wav at 48 kHz.
"""

import os, time, wave, threading, datetime, sys
import numpy as np, pyaudio, RPi.GPIO as GPIO


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from config import SAMPLE_RATE, NUM_CHANNELS, FRAME_CHUNK, MIC_DEVICE_INDEX, \
                   GPIO_BUTTON_PIN, GPIO_LED_PIN

GPIO.setwarnings(False)
GPIO.cleanup()
GPIO.setmode(GPIO.BCM)
GPIO.setup(GPIO_BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(GPIO_LED_PIN,    GPIO.OUT, initial=GPIO.LOW)

pa = pyaudio.PyAudio()
stream, frames, recording = None, [], False
lock = threading.Lock()

def capture():
    global frames
    while recording:
        frames.append(stream.read(FRAME_CHUNK, exception_on_overflow=False))

def toggle(channel):
    global stream, frames, recording
    with lock:
        if not recording:
            stream = pa.open(format=pyaudio.paInt16, channels=NUM_CHANNELS,
                             rate=SAMPLE_RATE, input=True,
                             frames_per_buffer=FRAME_CHUNK,
                             input_device_index=MIC_DEVICE_INDEX)
            frames, recording = [], True
            GPIO.output(GPIO_LED_PIN, GPIO.HIGH)
            threading.Thread(target=capture, daemon=True).start()
            print("🔴 Recording…")
        else:
            recording = False
            GPIO.output(GPIO_LED_PIN, GPIO.LOW)
            time.sleep(0.1)
            stream.stop_stream(); stream.close()
            save(frames)
            print("✅ Saved")

def save(fr):
    os.makedirs("recordings", exist_ok=True)
    raw = b"".join(fr)
    audio = np.frombuffer(raw, np.int16)
    if audio.size and (pk:=np.max(np.abs(audio))):
        g=int(0.9*32767/pk);  audio=np.clip(audio*g,-32768,32767).astype(np.int16)
    fname = f"recordings/rec_{datetime.datetime.now():%Y%m%d-%H%M%S}.wav"
    with wave.open(fname,"wb") as wf:
        wf.setnchannels(NUM_CHANNELS)
        wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    print(fname)

GPIO.add_event_detect(GPIO_BUTTON_PIN, GPIO.FALLING, callback=toggle, bouncetime=200)
print("▶️ Ready. Press the button to record. Ctrl‑C to quit.")

try:
    while True: time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    pa.terminate(); GPIO.cleanup()
