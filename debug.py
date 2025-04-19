# debug.py

#!/usr/bin/env python3
"""
Debug endpoints for testing tones, LED, button, and mic recording.
"""

import os
import time
import threading
import logging
import wave

from flask import Blueprint, current_app, jsonify, request
from pydub.generators import Sine

import pyaudio
import numpy as np

from config import (
    GPIO_LED_PIN,
    GPIO_BUTTON_PIN,
    BUTTON_ACTIVE_HIGH,
    MIC_DEVICE_INDEX,
    MIC_SAMPLE_RATE,
    MIC_CHANNELS,
    MIC_CHUNK,
)

# Try to import GPIO
try:
    import RPi.GPIO as GPIO
    HAS_GPIO = True
except ImportError:
    HAS_GPIO = False

log = logging.getLogger("debug")
bp = Blueprint("debug", __name__, url_prefix="/api/debug")

# directory for saving generated/recorded audio (reuse main audio folder)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "audio_files")
os.makedirs(AUDIO_DIR, exist_ok=True)

# initialize GPIO if available
if HAS_GPIO:
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(GPIO_LED_PIN, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(GPIO_BUTTON_PIN, GPIO.IN)

@bp.route("/tone", methods=["POST"])
def tone():
    """
    Generate a sine tone and play it.
    JSON body params:
      - frequency (Hz, default=440)
      - duration (ms, default=1000)
    Returns {"audio_url": "..."} for playback.
    """
    data = request.get_json() or {}
    freq = float(data.get("frequency", 440))
    duration = int(data.get("duration", 1000))
    filename = f"debug_tone_{int(freq)}Hz_{duration}ms.wav"
    filepath = os.path.join(AUDIO_DIR, filename)

    # generate tone
    seg = Sine(freq).to_audio_segment(duration=duration).set_frame_rate(44100).set_channels(1).set_sample_width(2)
    seg.export(filepath, format="wav")

    # play it immediately
    from audio_manager import play_audio
    play_audio(filepath)

    return jsonify({"audio_url": f"/audio_files/{filename}"})

@bp.route("/led", methods=["POST"])
def led():
    """
    Blink the LED.
    JSON body params:
      - times (int, default=3)
      - interval (sec, default=0.5)
    """
    if not HAS_GPIO:
        return jsonify({"error": "GPIO not available"}), 400

    data = request.get_json() or {}
    times = int(data.get("times", 3))
    interval = float(data.get("interval", 0.5))

    def blink():
        for _ in range(times):
            GPIO.output(GPIO_LED_PIN, GPIO.HIGH if BUTTON_ACTIVE_HIGH else GPIO.LOW)
            time.sleep(interval)
            GPIO.output(GPIO_LED_PIN, GPIO.LOW if BUTTON_ACTIVE_HIGH else GPIO.HIGH)
            time.sleep(interval)

    threading.Thread(target=blink, daemon=True).start()
    return jsonify({"message": f"Blinking LED {times} times at {interval}s interval"})

@bp.route("/button", methods=["GET"])
def button():
    """
    Read the current button state.
    Returns {"pressed": true/false}.
    """
    if not HAS_GPIO:
        return jsonify({"error": "GPIO not available"}), 400

    raw = GPIO.input(GPIO_BUTTON_PIN)
    pressed = bool(raw) if BUTTON_ACTIVE_HIGH else not bool(raw)
    return jsonify({"pressed": pressed})

@bp.route("/mic", methods=["POST"])
def mic():
    """
    Record from the USB mic.
    JSON body params:
      - duration (sec, default=3)
    Returns {"audio_url": "..."} for playback.
    """
    data = request.get_json() or {}
    duration = float(data.get("duration", 3))
    ts = int(time.time())
    filename = f"debug_mic_{ts}.wav"
    filepath = os.path.join(AUDIO_DIR, filename)

    pa = pyaudio.PyAudio()
    fmt = pyaudio.paInt16
    channels = MIC_CHANNELS
    rate = (
        MIC_SAMPLE_RATE
        if MIC_SAMPLE_RATE and MIC_SAMPLE_RATE != 0
        else int(pa.get_device_info_by_index(MIC_DEVICE_INDEX)["defaultSampleRate"])
    )
    chunk = MIC_CHUNK

    stream = pa.open(
        format=fmt,
        channels=channels,
        rate=rate,
        input=True,
        frames_per_buffer=chunk,
        input_device_index=MIC_DEVICE_INDEX,
    )
    frames = []
    total_chunks = int(rate / chunk * duration)
    for _ in range(total_chunks):
        try:
            data_chunk = stream.read(chunk, exception_on_overflow=False)
        except Exception:
            break
        frames.append(data_chunk)

    stream.stop_stream()
    stream.close()
    pa.terminate()

    wf = wave.open(filepath, "wb")
    wf.setnchannels(channels)
    wf.setsampwidth(pa.get_sample_size(fmt))
    wf.setframerate(rate)
    wf.writeframes(b"".join(frames))
    wf.close()

    # play it immediately
    from audio_manager import play_audio
    play_audio(filepath)

    return jsonify({"audio_url": f"/audio_files/{filename}"})
