#!/usr/bin/env python3
"""
Simplified Debug endpoints: tone, LED, button, mic start/stop.
Ensures correct sample rate metadata in saved WAV files.
Handles GPIO setup more robustly.
"""

import os
import time
import threading
import logging
import wave
from flask import Blueprint, jsonify, request
# pydub is only needed for tone generation here, not playback resampling
from pydub import AudioSegment, exceptions as pydub_exceptions
from pydub.generators import Sine
import pyaudio

from config import (
    GPIO_LED_PIN, GPIO_BUTTON_PIN, BUTTON_ACTIVE_HIGH, ENABLE_GPIO,
    MIC_DEVICE_INDEX, MIC_SAMPLE_RATE, MIC_CHANNELS, MIC_CHUNK,
    OUTPUT_SAMPLE_RATE # Needed for tone generation consistency? Maybe not critical here.
)
# Use audio_manager's player which handles resampling to target rate
from audio_manager import play_audio

log = logging.getLogger("debug") # Use specific logger

# --- GPIO Setup (Conditional and More Robust) ---
HAS_GPIO = False
GPIO = None # Initialize GPIO to None

if ENABLE_GPIO:
    try:
        import RPi.GPIO as RPiGPIO # Use alias
        GPIO = RPiGPIO # Assign to global variable AFTER successful import

        # *** Set mode JUST BEFORE setting up pins ***
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False) # Suppress channel already in use warnings
        log.info("GPIO Mode set to BCM and warnings disabled.")

        # Setup pins only if they are valid (e.g., > 0) and setmode succeeded
        setup_ok = True # Assume success initially

        # Setup LED Pin
        if GPIO_LED_PIN > 0:
             try:
                 GPIO.setup(GPIO_LED_PIN, GPIO.OUT, initial=GPIO.LOW)
                 log.info(f"GPIO LED Pin ({GPIO_LED_PIN}) setup as OUT.")
             except Exception as e_setup:
                 log.error(f"Failed to setup GPIO LED Pin ({GPIO_LED_PIN}): {e_setup}")
                 setup_ok = False
        else:
             log.info("GPIO LED Pin not configured (<= 0).")

        # Setup Button Pin (only if LED setup was okay or not needed)
        if GPIO_BUTTON_PIN > 0:
            if setup_ok: # Proceed only if previous steps were fine
                 try:
                     # Determine pull resistor based on config's ACTIVE_HIGH setting
                     # Pull UP if active LOW (button connects pin to GND)
                     # Pull DOWN if active HIGH (button connects pin to 3V3)
                     pull_resistor = GPIO.PUD_UP if not BUTTON_ACTIVE_HIGH else GPIO.PUD_DOWN
                     GPIO.setup(GPIO_BUTTON_PIN, GPIO.IN, pull_up_down=pull_resistor)
                     log.info(f"GPIO Button Pin ({GPIO_BUTTON_PIN}) setup as IN (Pull {'UP' if pull_resistor == GPIO.PUD_UP else 'DOWN'}).")
                 except Exception as e_setup:
                      log.error(f"Failed to setup GPIO Button Pin ({GPIO_BUTTON_PIN}): {e_setup}")
                      setup_ok = False
            else:
                 log.warning(f"Skipping Button Pin ({GPIO_BUTTON_PIN}) setup due to previous error.")
        else:
            log.info("GPIO Button Pin not configured (<= 0).")


        # Final check: Only enable GPIO features if setup completed successfully
        if setup_ok and (GPIO_LED_PIN > 0 or GPIO_BUTTON_PIN > 0):
             HAS_GPIO = True # Set True only if import, setmode, AND setup for at least one pin worked
             log.info("GPIO initialized successfully for debug endpoints.")
        elif not (GPIO_LED_PIN > 0 or GPIO_BUTTON_PIN > 0):
             log.info("GPIO enabled, but no valid LED or Button pins configured for debug.")
             HAS_GPIO = False # No usable pins
             GPIO = None
        else:
             log.warning("GPIO initialization failed during pin setup. Debug Button/LED disabled.")
             # Attempt cleanup if setup failed partially
             try: GPIO.cleanup()
             except Exception: pass
             HAS_GPIO = False # Ensure it's false
             GPIO = None # Ensure GPIO object is None if setup failed

    except ImportError:
        log.warning("RPi.GPIO library not found. Debug Button/LED disabled.")
        HAS_GPIO = False
        GPIO = None
    except RuntimeError as e: # Catch potential setmode errors etc.
        log.error(f"GPIO Runtime Error during initialization: {e}. Debug Button/LED disabled.")
        HAS_GPIO = False
        GPIO = None
    except Exception as e: # Catch any other unexpected errors
        log.exception(f"Unexpected error during GPIO initialization: {e}. Debug Button/LED disabled.")
        # Attempt cleanup just in case
        if GPIO:
            try: GPIO.cleanup()
            except Exception: pass
        HAS_GPIO = False
        GPIO = None

else:
    log.info("GPIO disabled by configuration or platform.")
# --- End GPIO Setup ---


bp = Blueprint("debug", __name__, url_prefix="/api/debug")

# Globals for mic recording state
_recording_thread = None
_recording_active = threading.Event() # Use Event for clearer signaling
_frames = []
_pa_debug = None
_stream_debug = None
_mic_rate_debug = 0 # Store sample rate used for recording

def _mic_worker_debug():
    """Dedicated thread for reading audio frames for debug recording."""
    global _frames, _stream_debug
    log.info("Debug Mic recording worker thread started.")
    frames_this_run = [] # Collect frames locally first
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
    freq = float(request.args.get("frequency", 440))
    dur_ms = int(request.args.get("duration", 500)) # Default 500ms
    audio_dir = os.path.join(os.getcwd(), "audio_files")
    os.makedirs(audio_dir, exist_ok=True)

    fname = f"debug_tone_{int(freq)}Hz_{dur_ms}ms.wav"
    path = os.path.join(audio_dir, fname)

    try:
        log.info(f"Generating tone: {freq} Hz, {dur_ms} ms")
        # Generate at the target output rate for consistency?
        # Or generate at 44.1k and let play_audio handle resampling?
        # Let's generate at target rate to minimize resampling steps.
        sine_wave = Sine(freq, sample_rate=OUTPUT_SAMPLE_RATE)
        audio_segment = sine_wave.to_audio_segment(duration=dur_ms)
        # Ensure 16-bit for compatibility
        audio_segment = audio_segment.set_sample_width(2)
        # Ensure Stereo (matching play_audio expectations)
        audio_segment = audio_segment.set_channels(2)
        audio_segment.export(path, format="wav")
        log.info(f"Tone saved to {path} (Rate: {OUTPUT_SAMPLE_RATE} Hz, Stereo)")

        # Play the generated tone using audio_manager (will use OUTPUT_SAMPLE_RATE)
        log.info("Playing generated tone via audio_manager...")
        play_audio(path) # This should now play correctly as it matches target rate
        log.info("Finished playing tone.")

        return jsonify({"status": "Tone generated and played", "audio_url": f"/audio_files/{fname}"})
    except Exception as e:
        log.exception("Error generating or playing tone.")
        return jsonify({"error": f"Failed to process tone: {e}"}), 500

@bp.route("/led", methods=["POST"])
def led():
    """Blink the LED 3×. Requires valid LED pin and GPIO enabled."""
    # Use a more robust check: HAS_GPIO must be True AND GPIO object must exist
    if not (HAS_GPIO and GPIO and GPIO_LED_PIN > 0):
        reason = "Unknown GPIO issue"
        if not GPIO: reason = "GPIO library object invalid"
        elif not HAS_GPIO: reason = "GPIO not available or initialization failed"
        elif GPIO_LED_PIN <= 0: reason = "LED pin not configured"
        log.warning(f"LED test endpoint called but GPIO is not ready: {reason}")
        return jsonify({"error": f"GPIO not ready: {reason}"}), 400

    def _blink():
        log.info(f"Blinking LED on pin {GPIO_LED_PIN}")
        try:
            # No need to setmode here again if initial setup worked
            for _ in range(3):
                GPIO.output(GPIO_LED_PIN, GPIO.HIGH)
                time.sleep(0.25)
                GPIO.output(GPIO_LED_PIN, GPIO.LOW)
                time.sleep(0.25)
            log.info("LED blink finished.")
        except Exception as e:
             log.error(f"Error during LED blink thread: {e}")
             try: GPIO.output(GPIO_LED_PIN, GPIO.LOW) # Ensure LED is off
             except Exception: pass

    threading.Thread(target=_blink, daemon=True).start()
    return jsonify({"status": "LED blink sequence started"})

@bp.route("/button", methods=["GET"])
def button():
    """Read current button state. Requires valid button pin and GPIO enabled."""
    # Use a more robust check
    if not (HAS_GPIO and GPIO and GPIO_BUTTON_PIN > 0):
        reason = "Unknown GPIO issue"
        if not GPIO: reason = "GPIO library object invalid"
        elif not HAS_GPIO: reason = "GPIO not available or initialization failed"
        elif GPIO_BUTTON_PIN <= 0: reason = "Button pin not configured"
        log.warning(f"Button read endpoint called but GPIO is not ready: {reason}")
        return jsonify({"error": f"GPIO not ready: {reason}"}), 400

    try:
        # No need to setmode here again if initial setup worked
        raw = GPIO.input(GPIO_BUTTON_PIN)
        # Logic based on config's BUTTON_ACTIVE_HIGH
        # pressed is True if raw state matches the 'active' state
        pressed = (raw == 1) if BUTTON_ACTIVE_HIGH else (raw == 0)
        log.debug(f"Button pin {GPIO_BUTTON_PIN} read: raw={raw}, pressed={pressed} (Active High: {BUTTON_ACTIVE_HIGH})")
        return jsonify({"pressed": pressed, "raw_value": raw}) # Return raw value too
    except Exception as e:
        log.error(f"Error reading button state: {e}")
        return jsonify({"error": f"Failed to read button: {e}"}), 500


@bp.route("/mic/start", methods=["POST"])
def mic_start():
    """Begin recording audio indefinitely until /mic/stop is called."""
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
        _stream_debug = _pa_debug.open(
            format=pyaudio.paInt16, channels=MIC_CHANNELS, rate=_mic_rate_debug,
            input=True, frames_per_buffer=MIC_CHUNK, input_device_index=MIC_DEVICE_INDEX,
            stream_callback=None
        )
        # Don't necessarily need start_stream() for blocking read
        # _stream_debug.start_stream()
        _recording_active.set()
        _recording_thread = threading.Thread(target=_mic_worker_debug, daemon=True)
        _recording_thread.start()
        log.info("Debug Mic recording started successfully.")
        return jsonify({"status": "Recording started"})

    except Exception as e:
        log.exception("Failed to start Debug Mic recording.")
        _recording_active.clear()
        if _stream_debug:
            try:
                 if _stream_debug.is_active(): _stream_debug.stop_stream()
                 _stream_debug.close()
            except Exception: pass
            _stream_debug = None
        if _pa_debug:
            try: _pa_debug.terminate()
            except Exception: pass
            _pa_debug = None
        _mic_rate_debug = 0
        return jsonify({"error": f"Failed to start recording: {e}"}), 500


@bp.route("/mic/stop", methods=["POST"])
def mic_stop():
    """Stop recording, save the WAV file with correct metadata, and return its URL."""
    global _recording_thread, _frames, _pa_debug, _stream_debug, _mic_rate_debug

    if not _recording_active.is_set():
        log.warning("Debug Mic stop requested, but not recording.")
        return jsonify({"error": "Not recording"}), 400

    log.info("Stopping Debug Mic recording...")
    _recording_active.clear() # Signal worker thread to stop

    if _recording_thread and _recording_thread.is_alive():
        log.debug("Waiting for debug recording thread to finish...")
        _recording_thread.join(timeout=1.5)
        if _recording_thread.is_alive(): log.warning("Debug recording thread did not finish cleanly.")
        _recording_thread = None

    if _stream_debug:
        try:
            if _stream_debug.is_active(): _stream_debug.stop_stream()
            _stream_debug.close(); log.debug("Debug Mic stream closed.")
        except Exception as e: log.error(f"Error closing debug mic stream: {e}")
        finally: _stream_debug = None

    if _pa_debug:
        try: _pa_debug.terminate(); log.debug("Debug PyAudio instance terminated.")
        except Exception as e: log.error(f"Error terminating debug PyAudio: {e}")
        finally: _pa_debug = None

    if not _frames:
        log.warning("Recording stopped, but no frames were captured."); _mic_rate_debug = 0
        return jsonify({"error": "No audio data recorded"}), 400

    if _mic_rate_debug <= 0:
         log.error("Cannot save WAV file: Invalid microphone sample rate captured during start.")
         _frames = []; return jsonify({"error": "Internal error: Invalid sample rate for saving."}), 500

    audio_dir = os.path.join(os.getcwd(), "audio_files")
    os.makedirs(audio_dir, exist_ok=True)
    ts = int(time.time())
    fname = f"debug_mic_{ts}.wav"
    path = os.path.join(audio_dir, fname)

    try:
        log.info(f"Saving debug recording to {path} (Rate: {_mic_rate_debug} Hz, Channels: {MIC_CHANNELS}, Width: 16-bit)")
        wf = wave.open(path, "wb")
        wf.setnchannels(MIC_CHANNELS)
        sample_width = pyaudio.PyAudio().get_sample_size(pyaudio.paInt16) # Should be 2
        wf.setsampwidth(sample_width)
        wf.setframerate(_mic_rate_debug) # Use the actual recording rate
        wf.writeframes(b"".join(_frames))
        wf.close()
        log.info(f"Debug recording saved successfully: {fname}")
        return jsonify({"status": "Recording stopped and saved", "audio_url": f"/audio_files/{fname}"})

    except Exception as e:
        log.exception("Failed to save debug microphone recording.")
        return jsonify({"error": f"Failed to save recording: {e}"}), 500
    finally:
        _frames = []; _mic_rate_debug = 0