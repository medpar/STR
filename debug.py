#!/usr/bin/env python3
"""
Simplified Debug endpoints: tone, LED, button, mic start/stop.
Ensures correct sample rate metadata in saved WAV files.
Handles GPIO setup more robustly by setting mode and setup within functions.
Records debug mic at fixed 48kHz and applies software gain.
"""

import os
import time
import threading
import logging
import wave
import numpy as np # Needed for mic gain
from flask import Blueprint, jsonify, request
# pydub is only needed for tone generation here, not playback resampling
from pydub import AudioSegment, exceptions as pydub_exceptions
from pydub.generators import Sine
import pyaudio

from config import (
    GPIO_LED_PIN, GPIO_BUTTON_PIN, BUTTON_ACTIVE_HIGH, ENABLE_GPIO,
    MIC_DEVICE_INDEX, MIC_CHANNELS, MIC_CHUNK, # MIC_SAMPLE_RATE no longer needed here
    OUTPUT_SAMPLE_RATE # Needed for tone generation
)
# Use audio_manager's player which handles resampling to target rate
from audio_manager import play_audio

log = logging.getLogger("debug") # Use specific logger

# --- Mic Gain Configuration ---
DEBUG_MIC_GAIN_FACTOR = 4.0

# --- GPIO Initial Check (Conditional) ---
# Only check if the library can be imported and if GPIO is enabled globally.
# Pin setup (mode and direction) will happen within each specific route/function.
HAS_GPIO = False
GPIO = None # Initialize GPIO to None

if ENABLE_GPIO:
    try:
        import RPi.GPIO as RPiGPIO # Use alias
        GPIO = RPiGPIO # Assign to global variable AFTER successful import
        HAS_GPIO = True # Mark as available if import succeeds
        log.info("RPi.GPIO library imported successfully for debug endpoints.")
        # DO NOT set mode or setup pins here globally for debug routes
    except ImportError:
        log.warning("RPi.GPIO library not found. Debug Button/LED disabled.")
        HAS_GPIO = False
        GPIO = None
    except Exception as e: # Catch any other unexpected errors during import
        log.exception(f"Unexpected error during GPIO import: {e}. Debug Button/LED disabled.")
        HAS_GPIO = False
        GPIO = None
else:
    log.info("GPIO disabled by configuration or platform.")
# --- End GPIO Initial Check ---


bp = Blueprint("debug", __name__, url_prefix="/api/debug")

# Globals for mic recording state (Unaffected by GPIO changes)
_recording_thread = None
_recording_active = threading.Event()
_frames = []
_pa_debug = None
_stream_debug = None
_mic_rate_debug = 48000 # Fixed rate for debug recording

def _mic_worker_debug():
    # (This function remains the same)
    global _frames, _stream_debug
    log.info(f"Debug Mic recording worker thread started (Target Rate: {_mic_rate_debug} Hz).")
    frames_this_run = []
    while _recording_active.is_set() and _stream_debug and _stream_debug.is_active():
        try:
            data = _stream_debug.read(MIC_CHUNK, exception_on_overflow=False)
            if data: frames_this_run.append(data)
        except OSError as e:
             if "Input overflowed" in str(e): log.warning("Debug Mic input overflow detected.")
             else: log.error(f"Debug Mic read OS error in worker: {e}"); _recording_active.clear(); break
        except Exception as e:
            log.exception("Unexpected error in debug mic recording worker."); _recording_active.clear(); break
    _frames.extend(frames_this_run)
    log.info(f"Debug Mic recording worker thread finished. Collected {len(frames_this_run)} frames.")


@bp.route("/tone", methods=["POST"])
def tone():
    # (This function remains the same)
    freq = float(request.args.get("frequency", 440)); dur_ms = int(request.args.get("duration", 500))
    audio_dir = os.path.join(os.getcwd(), "audio_files"); os.makedirs(audio_dir, exist_ok=True)
    fname = f"debug_tone_{int(freq)}Hz_{dur_ms}ms.wav"; path = os.path.join(audio_dir, fname)
    try:
        log.info(f"Generating tone: {freq} Hz, {dur_ms} ms")
        sine_wave = Sine(freq, sample_rate=44100); audio_segment = sine_wave.to_audio_segment(duration=dur_ms)
        audio_segment = audio_segment.set_sample_width(2); audio_segment.export(path, format="wav")
        log.info(f"Tone saved to {path} (Rate: 44100 Hz, Mono)")
        log.info("Playing generated tone via audio_manager..."); play_audio(path); log.info("Finished playing tone.")
        return jsonify({"status": "Tone generated and played", "audio_url": f"/audio_files/{fname}"})
    except Exception as e:
        log.exception("Error generating or playing tone."); return jsonify({"error": f"Failed to process tone: {e}"}), 500

@bp.route("/led", methods=["POST"])
def led():
    """Blink the LED 3×. Requires valid LED pin and GPIO enabled."""
    # Check if GPIO library is available and the specific pin is configured
    if not (HAS_GPIO and GPIO and GPIO_LED_PIN > 0):
        reason = "Unknown GPIO issue"
        if not GPIO: reason = "GPIO library object invalid"
        elif not HAS_GPIO: reason = "GPIO library failed import"
        elif GPIO_LED_PIN <= 0: reason = "LED pin not configured"
        log.warning(f"LED test endpoint called but GPIO is not ready: {reason}")
        return jsonify({"error": f"GPIO not ready: {reason}"}), 400

    def _blink():
        log.info(f"Blinking LED on pin {GPIO_LED_PIN}")
        led_pin = GPIO_LED_PIN # Local variable for clarity
        try:
            # *** FIX: Set mode AND setup pin within the thread ***
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            GPIO.setup(led_pin, GPIO.OUT) # <<< Setup as OUTPUT here
            log.debug(f"LED Pin {led_pin} set up as OUT inside thread.")

            # Perform the blink
            for _ in range(3):
                GPIO.output(led_pin, GPIO.HIGH)
                time.sleep(0.25)
                GPIO.output(led_pin, GPIO.LOW)
                time.sleep(0.25)
            log.info("LED blink finished.")
        except Exception as e:
             log.error(f"Error during LED blink thread: {e}")
        finally:
             # Optional: Add cleanup specific to this temporary setup
             try:
                 # Ensure LED is left LOW
                 GPIO.output(led_pin, GPIO.LOW)
                 # Cleanup the pin we just set up
                 GPIO.cleanup(led_pin)
                 log.debug(f"Cleaned up LED pin {led_pin} in thread.")
             except Exception as e_clean:
                 # Log cleanup errors but don't crash
                 log.error(f"Error cleaning up LED pin {led_pin} in thread: {e_clean}")


    threading.Thread(target=_blink, daemon=True).start()
    return jsonify({"status": "LED blink sequence started"})

@bp.route("/button", methods=["GET"])
def button():
    """Read current button state. Requires valid button pin and GPIO enabled."""
    # Check if GPIO library is available and the specific pin is configured
    if not (HAS_GPIO and GPIO and GPIO_BUTTON_PIN > 0):
        reason = "Unknown GPIO issue"
        if not GPIO: reason = "GPIO library object invalid"
        elif not HAS_GPIO: reason = "GPIO library failed import"
        elif GPIO_BUTTON_PIN <= 0: reason = "Button pin not configured"
        log.warning(f"Button read endpoint called but GPIO is not ready: {reason}")
        return jsonify({"error": f"GPIO not ready: {reason}"}), 400

    button_pin = GPIO_BUTTON_PIN # Local variable
    try:
        # *** FIX: Set mode AND setup pin within the route handler ***
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        pull_resistor = GPIO.PUD_UP if not BUTTON_ACTIVE_HIGH else GPIO.PUD_DOWN
        GPIO.setup(button_pin, GPIO.IN, pull_up_down=pull_resistor) # <<< Setup as INPUT here
        log.debug(f"Button Pin {button_pin} set up as IN inside route handler (Pull {'UP' if pull_resistor == GPIO.PUD_UP else 'DOWN'}).")

        # Read the input
        raw = GPIO.input(button_pin)
        pressed = (raw == 1) if BUTTON_ACTIVE_HIGH else (raw == 0)
        log.debug(f"Button pin {button_pin} read: raw={raw}, pressed={pressed} (Active High: {BUTTON_ACTIVE_HIGH})")

        # Cleanup the pin after reading
        GPIO.cleanup(button_pin)
        log.debug(f"Cleaned up button pin {button_pin} in route handler.")

        return jsonify({"pressed": pressed, "raw_value": raw})

    except Exception as e:
        log.error(f"Error reading button state: {e}")
        # Attempt cleanup even on error
        try:
            GPIO.cleanup(button_pin)
            log.debug(f"Cleaned up button pin {button_pin} after error.")
        except Exception as e_clean:
            log.error(f"Error cleaning up button pin {button_pin} after error: {e_clean}")
        return jsonify({"error": f"Failed to read button: {e}"}), 500


@bp.route("/mic/start", methods=["POST"])
def mic_start():
    # (This function remains the same - mic part was working)
    global _recording_thread, _frames, _pa_debug, _stream_debug, _mic_rate_debug
    if _recording_active.is_set(): log.warning("..."); return jsonify({"error": "Already recording"}), 400
    _frames = []; _mic_rate_debug = 48000
    log.info(f"Attempting to start Debug Mic recording at fixed rate: {_mic_rate_debug} Hz")
    try:
        _pa_debug = pyaudio.PyAudio()
        _stream_debug = _pa_debug.open(
            format=pyaudio.paInt16, channels=MIC_CHANNELS, rate=_mic_rate_debug,
            input=True, frames_per_buffer=MIC_CHUNK, input_device_index=MIC_DEVICE_INDEX,
            stream_callback=None
        )
        _recording_active.set(); _recording_thread = threading.Thread(target=_mic_worker_debug, daemon=True); _recording_thread.start()
        log.info(f"Debug Mic recording started successfully at {_mic_rate_debug} Hz.")
        return jsonify({"status": "Recording started"})
    except Exception as e:
        log.exception("Failed to start Debug Mic recording.")
        _recording_active.clear()
        if _stream_debug:
            try: _stream_debug.close() # Add try-except
            except Exception: pass
            _stream_debug = None
        if _pa_debug:
            try: _pa_debug.terminate() # Add try-except
            except Exception: pass
            _pa_debug = None
        return jsonify({"error": f"Failed to start recording: {e}"}), 500


@bp.route("/mic/stop", methods=["POST"])
def mic_stop():
    # (This function remains the same - mic part was working)
    global _recording_thread, _frames, _pa_debug, _stream_debug, _mic_rate_debug
    if not _recording_active.is_set(): log.warning("..."); return jsonify({"error": "Not recording"}), 400
    log.info("Stopping Debug Mic recording..."); _recording_active.clear()
    if _recording_thread and _recording_thread.is_alive():
        _recording_thread.join(timeout=1.5) # ... (join logic) ...
        if _recording_thread.is_alive(): log.warning("Debug recording thread did not finish cleanly.")
        _recording_thread = None # Clear thread reference
    if _stream_debug:
        try: _stream_debug.close() # Add try-except
        except Exception as e: log.error(f"Error closing debug mic stream: {e}")
        finally: _stream_debug = None # Ensure cleared
    if _pa_debug:
        try: _pa_debug.terminate() # Add try-except
        except Exception as e: log.error(f"Error terminating debug PyAudio: {e}")
        finally: _pa_debug = None # Ensure cleared
    if not _frames: log.warning("..."); return jsonify({"error": "No audio data recorded"}), 400
    if _mic_rate_debug <= 0: log.error("..."); _frames = []; return jsonify({"error": "Internal error..."}), 500
    audio_dir = os.path.join(os.getcwd(), "audio_files"); os.makedirs(audio_dir, exist_ok=True)
    ts = int(time.time()); fname = f"debug_mic_{ts}.wav"; path = os.path.join(audio_dir, fname)
    try:
        raw_data = b"".join(_frames); _frames = []
        processed_data = raw_data
        if DEBUG_MIC_GAIN_FACTOR != 1.0 and raw_data:
            log.info(f"Applying software gain (factor: {DEBUG_MIC_GAIN_FACTOR:.2f})...")
            try:
                samples = np.frombuffer(raw_data, dtype=np.int16)
                amplified_samples = samples.astype(np.float32) * DEBUG_MIC_GAIN_FACTOR
                np.clip(amplified_samples, -32768, 32767, out=amplified_samples)
                processed_data = amplified_samples.astype(np.int16).tobytes()
                log.info("Gain applied successfully.")
            except Exception as e_gain: log.error(f"Error applying gain: {e_gain}. Saving original."); processed_data = raw_data
        else: log.info("Skipping software gain."); processed_data = raw_data
        log.info(f"Saving debug recording to {path} (Rate: {_mic_rate_debug} Hz ...)")
        wf = wave.open(path, "wb"); wf.setnchannels(MIC_CHANNELS)
        sample_width = pyaudio.PyAudio().get_sample_size(pyaudio.paInt16); wf.setsampwidth(sample_width)
        wf.setframerate(_mic_rate_debug); wf.writeframes(processed_data); wf.close()
        log.info(f"Debug recording saved successfully: {fname}")
        return jsonify({"status": "Recording stopped and saved", "audio_url": f"/audio_files/{fname}"})
    except Exception as e: log.exception("..."); return jsonify({"error": f"Failed to save recording: {e}"}), 500
    finally: _frames = [] # Ensure frames cleared