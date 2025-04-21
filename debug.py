#!/usr/bin/env python3
"""
Simplified Debug endpoints: tone, LED, button, mic start/stop.
Ensures correct sample rate metadata in saved WAV files.
Sets GPIO mode explicitly in relevant functions.
"""

import os
import time
import threading
import logging
import wave
from flask import Blueprint, jsonify, request
# Keep pydub for tone generation
from pydub import AudioSegment, exceptions as pydub_exceptions
from pydub.generators import Sine
import pyaudio

from config import (
    GPIO_LED_PIN, GPIO_BUTTON_PIN, BUTTON_ACTIVE_HIGH, ENABLE_GPIO,
    MIC_DEVICE_INDEX, MIC_SAMPLE_RATE, MIC_CHANNELS, MIC_CHUNK
)
from audio_manager import play_audio # Re-import for tone playback

log = logging.getLogger("debug") # Use specific logger

# Conditional GPIO Import
try:
    import RPi.GPIO as GPIO
    # Note: setmode is now called within functions needing it
    HAS_GPIO = ENABLE_GPIO # Assume available if enabled and import worked
    if HAS_GPIO:
        log.info("RPi.GPIO imported successfully for debug.")
    else:
        log.info("GPIO disabled by configuration (ENABLE_GPIO=False).")
except (ImportError, RuntimeError):
    GPIO = None
    HAS_GPIO = False
    if ENABLE_GPIO: # Log warning only if it was expected to work
        log.warning("RPi.GPIO import failed, but ENABLE_GPIO=True. GPIO debug disabled.")
    else:
        log.info("GPIO disabled (RPi.GPIO not found or not RPi).")


bp = Blueprint("debug", __name__, url_prefix="/api/debug")

# Globals for mic recording state (remain the same)
_recording_thread = None
_recording_active = threading.Event()
_frames = []
_pa_debug = None
_stream_debug = None
_mic_rate_debug = 0

def _mic_worker_debug():
    """Dedicated thread for reading audio frames for debug recording."""
    # --- This function remains the same ---
    global _frames, _stream_debug
    log.info("Debug Mic recording worker thread started.")
    frames_this_run = []
    while _recording_active.is_set() and _stream_debug and _stream_debug.is_active():
        try:
            data = _stream_debug.read(MIC_CHUNK, exception_on_overflow=False)
            if data:
                 frames_this_run.append(data)
        except OSError as e:
             if "Input overflowed" in str(e): log.warning("Debug Mic input overflow detected.")
             else: log.error(f"Debug Mic read OS error in worker: {e}"); _recording_active.clear(); break
        except Exception as e:
            log.exception("Unexpected error in debug mic recording worker."); _recording_active.clear(); break
    _frames.extend(frames_this_run)
    log.info(f"Debug Mic recording worker thread finished. Collected {len(frames_this_run)} frames.")


@bp.route("/tone", methods=["POST"])
def tone():
    """
    Generate and save a sine tone WAV file. Plays it and returns the URL.
    Query args: frequency (Hz), duration (ms)
    """
    # --- This function remains the same ---
    freq = float(request.args.get("frequency", 440))
    dur_ms = int(request.args.get("duration", 500))
    audio_dir = os.path.join(os.getcwd(), "audio_files")
    os.makedirs(audio_dir, exist_ok=True)
    fname = f"debug_tone_{int(freq)}Hz_{dur_ms}ms.wav"
    path = os.path.join(audio_dir, fname)
    try:
        log.info(f"Generating tone: {freq} Hz, {dur_ms} ms")
        sine_wave = Sine(freq)
        audio_segment = sine_wave.to_audio_segment(duration=dur_ms).set_sample_width(2)
        audio_segment.export(path, format="wav")
        log.info(f"Tone saved to {path}")
        log.info("Playing generated tone via audio_manager...")
        play_audio(path)
        log.info("Finished playing tone.")
        return jsonify({"status": "Tone generated and played", "audio_url": f"/audio_files/{fname}"})
    except Exception as e:
        log.exception("Error generating or playing tone.")
        return jsonify({"error": f"Failed to process tone: {e}"}), 500

@bp.route("/led", methods=["POST"])
def led():
    """Blink the LED 3×. Requires valid LED pin and GPIO enabled."""
    if not HAS_GPIO or GPIO_LED_PIN <= 0:
        return jsonify({"error": "GPIO not available or LED pin not configured"}), 400

    def _blink():
        try:
            # *** ADDED: Set mode and warnings within the thread ***
            log.debug(f"Setting GPIO mode to BCM for LED blink (Pin {GPIO_LED_PIN}).")
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            # Ensure pin is setup as output
            GPIO.setup(GPIO_LED_PIN, GPIO.OUT, initial=GPIO.LOW) # Re-setup just in case
            log.info(f"Blinking LED on pin {GPIO_LED_PIN}")
            for _ in range(3):
                GPIO.output(GPIO_LED_PIN, GPIO.HIGH)
                time.sleep(0.25)
                GPIO.output(GPIO_LED_PIN, GPIO.LOW)
                time.sleep(0.25)
            log.info("LED blink finished.")
            # Note: No GPIO.cleanup() here to avoid interfering with other usages
        except Exception as e:
             log.error(f"Error during LED blink thread: {e}")
             # Attempt to ensure LED is off on error, assuming setup worked
             try:
                 # Check if setup might have failed before outputting
                 # A more robust check would involve checking GPIO state if possible
                 GPIO.output(GPIO_LED_PIN, GPIO.LOW)
             except Exception: pass

    threading.Thread(target=_blink, daemon=True).start()
    return jsonify({"status": "LED blink sequence started"})

@bp.route("/button", methods=["GET"])
def button():
    """Read current button state. Requires valid button pin and GPIO enabled."""
    if not HAS_GPIO or GPIO_BUTTON_PIN <= 0:
        return jsonify({"error": "GPIO not available or Button pin not configured"}), 400

    try:
        # *** ADDED: Set mode and warnings before reading ***
        log.debug(f"Setting GPIO mode to BCM for Button read (Pin {GPIO_BUTTON_PIN}).")
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        # Ensure pin is setup as input with correct pull resistor
        pull_resistor = GPIO.PUD_DOWN if BUTTON_ACTIVE_HIGH else GPIO.PUD_UP # Pull DOWN for active high
        # User mentioned pull down resistor, so BUTTON_ACTIVE_HIGH should be True
        # Let's adjust pull-up/down based on BUTTON_ACTIVE_HIGH setting from config
        # pull_resistor = GPIO.PUD_UP if not BUTTON_ACTIVE_HIGH else GPIO.PUD_DOWN
        log.debug(f"Configuring Button Pin {GPIO_BUTTON_PIN} as IN, Pull {'DOWN' if pull_resistor == GPIO.PUD_DOWN else 'UP'}")
        GPIO.setup(GPIO_BUTTON_PIN, GPIO.IN, pull_up_down=pull_resistor) # Re-setup just in case

        raw = GPIO.input(GPIO_BUTTON_PIN)
        # Logic based on config's BUTTON_ACTIVE_HIGH
        # If ACTIVE_HIGH=True, pressed state is 1 (HIGH).
        # If ACTIVE_HIGH=False, pressed state is 0 (LOW).
        pressed = (raw == GPIO.HIGH) if BUTTON_ACTIVE_HIGH else (raw == GPIO.LOW)
        log.debug(f"Button pin {GPIO_BUTTON_PIN} read: raw={raw}, pressed={pressed} (Active High Config: {BUTTON_ACTIVE_HIGH})")
        return jsonify({"pressed": pressed})
        # Note: No GPIO.cleanup() here
    except Exception as e:
        log.error(f"Error reading button state: {e}")
        return jsonify({"error": f"Failed to read button: {e}"}), 500


@bp.route("/mic/start", methods=["POST"])
def mic_start():
    """Begin recording audio indefinitely until /mic/stop is called."""
    # --- This function remains the same ---
    global _recording_thread, _frames, _pa_debug, _stream_debug, _mic_rate_debug
    if _recording_active.is_set():
        log.warning("Debug Mic start requested, but already recording.")
        return jsonify({"error": "Already recording"}), 400
    _frames = []; _mic_rate_debug = 0
    try:
        _pa_debug = pyaudio.PyAudio()
        device_info = _pa_debug.get_device_info_by_index(MIC_DEVICE_INDEX)
        _mic_rate_debug = MIC_SAMPLE_RATE if MIC_SAMPLE_RATE > 0 else int(device_info["defaultSampleRate"])
        if _mic_rate_debug <= 0: raise ValueError(f"Invalid mic rate detected: {_mic_rate_debug}")
        log.info(f"Starting Debug Mic recording: Index={MIC_DEVICE_INDEX}, Rate={_mic_rate_debug} Hz, Channels={MIC_CHANNELS}")
        _stream_debug = _pa_debug.open(format=pyaudio.paInt16, channels=MIC_CHANNELS, rate=_mic_rate_debug, input=True, frames_per_buffer=MIC_CHUNK, input_device_index=MIC_DEVICE_INDEX, stream_callback=None)
        _stream_debug.start_stream()
        _recording_active.set()
        _recording_thread = threading.Thread(target=_mic_worker_debug, daemon=True); _recording_thread.start()
        log.info("Debug Mic recording started successfully.")
        return jsonify({"status": "Recording started"})
    except Exception as e:
        log.exception("Failed to start Debug Mic recording.")
        _recording_active.clear()
        if _stream_debug: try: _stream_debug.stop_stream(); _stream_debug.close() except Exception: pass; _stream_debug = None
        if _pa_debug: try: _pa_debug.terminate() except Exception: pass; _pa_debug = None
        _mic_rate_debug = 0
        return jsonify({"error": f"Failed to start recording: {e}"}), 500


@bp.route("/mic/stop", methods=["POST"])
def mic_stop():
    """Stop recording, save the WAV file with correct metadata, and return its URL."""
    # --- This function remains the same ---
    global _recording_thread, _frames, _pa_debug, _stream_debug, _mic_rate_debug
    if not _recording_active.is_set():
        log.warning("Debug Mic stop requested, but not recording.")
        return jsonify({"error": "Not recording"}), 400
    log.info("Stopping Debug Mic recording..."); _recording_active.clear()
    if _recording_thread and _recording_thread.is_alive():
        log.debug("Waiting for debug recording thread to finish..."); _recording_thread.join(timeout=1.5)
        if _recording_thread.is_alive(): log.warning("Debug recording thread did not finish cleanly.")
        _recording_thread = None
    if _stream_debug: try: _stream_debug.stop_stream(); _stream_debug.close(); log.debug("Debug Mic stream closed.") except Exception as e: log.error(f"Error closing debug mic stream: {e}"); _stream_debug = None
    if _pa_debug: try: _pa_debug.terminate(); log.debug("Debug PyAudio instance terminated.") except Exception as e: log.error(f"Error terminating debug PyAudio: {e}"); _pa_debug = None
    if not _frames: log.warning("Recording stopped, but no frames were captured."); _mic_rate_debug = 0; return jsonify({"error": "No audio data recorded"}), 400
    if _mic_rate_debug <= 0: log.error("Cannot save WAV file: Invalid microphone sample rate."); _frames = []; return jsonify({"error": "Internal error: Invalid sample rate for saving."}), 500
    audio_dir = os.path.join(os.getcwd(), "audio_files"); os.makedirs(audio_dir, exist_ok=True)
    ts = int(time.time()); fname = f"debug_mic_{ts}.wav"; path = os.path.join(audio_dir, fname)
    try:
        log.info(f"Saving debug recording to {path} (Rate: {_mic_rate_debug} Hz, Channels: {MIC_CHANNELS}, Width: 16-bit)")
        wf = wave.open(path, "wb"); wf.setnchannels(MIC_CHANNELS)
        sample_width = pyaudio.PyAudio().get_sample_size(pyaudio.paInt16) # Get width for paInt16
        wf.setsampwidth(sample_width); wf.setframerate(_mic_rate_debug); wf.writeframes(b"".join(_frames)); wf.close()
        log.info(f"Debug recording saved successfully: {fname}")
        return jsonify({"status": "Recording stopped and saved", "audio_url": f"/audio_files/{fname}"})
    except Exception as e: log.exception("Failed to save debug microphone recording."); return jsonify({"error": f"Failed to save recording: {e}"}), 500
    finally: _frames = []; _mic_rate_debug = 0