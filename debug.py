#!/usr/bin/env python3
"""
Simplified Debug endpoints: tone, LED, button, mic start/stop.
"""

import os, time, threading, logging, wave
from flask import Blueprint, jsonify
from pydub.generators import Sine
import pyaudio

from config import (
    GPIO_LED_PIN, GPIO_BUTTON_PIN, BUTTON_ACTIVE_HIGH,
    MIC_DEVICE_INDEX, MIC_SAMPLE_RATE, MIC_CHANNELS, MIC_CHUNK
)
from audio_manager import play_audio

try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(GPIO_LED_PIN, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(GPIO_BUTTON_PIN, GPIO.IN)
    HAS_GPIO = True
except ImportError:
    HAS_GPIO = False
    logging.getLogger(__name__).warning("GPIO not available – debug button/LED disabled")

bp = Blueprint("debug", __name__, url_prefix="/api/debug")

# Globals for mic recording
_recording = False
_frames = []
_pa = None
_stream = None

def _mic_worker():
    global _recording, _stream, _frames
    while _recording:
        try:
            data = _stream.read(MIC_CHUNK, exception_on_overflow=False)
            _frames.append(data)
        except Exception:
            break

@bp.route("/tone", methods=["POST"])
def tone():
    """
    Play a sine tone.
    Query args: frequency (Hz), duration (sec)
    """
    from flask import request
    freq = float(request.args.get("frequency", 440))
    dur = float(request.args.get("duration", 1))
    fname = f"debug_tone_{int(freq)}Hz_{int(dur)}s.wav"
    path = os.path.join(os.getcwd(), "audio_files", fname)
    seg = Sine(freq).to_audio_segment(duration=int(dur*1000))
    seg.export(path, format="wav")
    play_audio(path)
    return jsonify({"audio_url": f"/audio_files/{fname}"})

@bp.route("/led", methods=["POST"])
def led():
    """Blink the LED 3× at 0.5s intervals."""
    if not HAS_GPIO:
        return jsonify({"error": "GPIO not available"}), 400

    def _blink():
        for _ in range(3):
            GPIO.output(GPIO_LED_PIN, GPIO.HIGH if BUTTON_ACTIVE_HIGH else GPIO.LOW)
            time.sleep(0.5)
            GPIO.output(GPIO_LED_PIN, GPIO.LOW if BUTTON_ACTIVE_HIGH else GPIO.HIGH)
            time.sleep(0.5)

    threading.Thread(target=_blink, daemon=True).start()
    return jsonify({"status": "LED blink started"})

@bp.route("/button", methods=["GET"])
def button():
    """Read current button state."""
    if not HAS_GPIO:
        return jsonify({"error": "GPIO not available"}), 400
    raw = GPIO.input(GPIO_BUTTON_PIN)
    pressed = bool(raw) if BUTTON_ACTIVE_HIGH else not bool(raw)
    return jsonify({"pressed": pressed})

@bp.route("/mic/start", methods=["POST"])
def mic_start():
    """Begin recording until /mic/stop is called."""
    global _recording, _frames, _pa, _stream
    if _recording:
        return jsonify({"error": "Already recording"}), 400

    _pa = pyaudio.PyAudio()
    rate = MIC_SAMPLE_RATE or int(_pa.get_device_info_by_index(MIC_DEVICE_INDEX)["defaultSampleRate"])
    _stream = _pa.open(
        format=pyaudio.paInt16,
        channels=MIC_CHANNELS,
        rate=rate,
        input=True,
        frames_per_buffer=MIC_CHUNK,
        input_device_index=MIC_DEVICE_INDEX,
    )
    _frames = []
    _recording = True
    threading.Thread(target=_mic_worker, daemon=True).start()
    return jsonify({"status": "recording started"})

@bp.route("/mic/stop", methods=["POST"])
def mic_stop():
    """Stop recording and play back."""
    global _recording, _frames, _pa, _stream
    if not _recording:
        return jsonify({"error": "Not recording"}), 400

    _recording = False
    time.sleep(0.1)  # let thread finish
    _stream.stop_stream()
    _stream.close()
    _pa.terminate()

    ts = int(time.time())
    fname = f"debug_mic_{ts}.wav"
    path = os.path.join(os.getcwd(), "audio_files", fname)
    wf = wave.open(path, "wb")
    wf.setnchannels(MIC_CHANNELS)
    wf.setsampwidth(_pa.get_sample_size(pyaudio.paInt16))
    wf.setframerate(rate)
    wf.writeframes(b"".join(_frames))
    wf.close()

    play_audio(path)
    return jsonify({"audio_url": f"/audio_files/{fname}"})
