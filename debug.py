#!/usr/bin/env python3
"""
Simplified Debug endpoints: tone, LED, button, mic start/stop.
Ensures correct sample rate metadata in saved WAV files.
Handles GPIO setup more robustly.
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
# Increase this value to make the debug mic recording louder.
# Sensible range might be 1.0 (no change) to 8.0. Start moderately.
# Be careful, high values will cause clipping (distortion).
DEBUG_MIC_GAIN_FACTOR = 4.0

# --- GPIO Setup (Conditional and More Robust) ---
# (Keep the initial setup block as before for HAS_GPIO check)
HAS_GPIO = False
GPIO = None # Initialize GPIO to None

if ENABLE_GPIO:
    try:
        import RPi.GPIO as RPiGPIO # Use alias
        GPIO = RPiGPIO # Assign to global variable AFTER successful import
        GPIO.setmode(GPIO.BCM) # Set mode globally first
        GPIO.setwarnings(False)
        log.info("GPIO Mode set to BCM and warnings disabled.")
        setup_ok = True
        if GPIO_LED_PIN > 0:
             try: GPIO.setup(GPIO_LED_PIN, GPIO.OUT, initial=GPIO.LOW); log.info(f"GPIO LED Pin ({GPIO_LED_PIN}) setup as OUT.")
             except Exception as e_setup: log.error(f"Failed to setup GPIO LED Pin ({GPIO_LED_PIN}): {e_setup}"); setup_ok = False
        else: log.info("GPIO LED Pin not configured (<= 0).")
        if GPIO_BUTTON_PIN > 0:
            if setup_ok:
                 try:
                     pull_resistor = GPIO.PUD_UP if not BUTTON_ACTIVE_HIGH else GPIO.PUD_DOWN
                     GPIO.setup(GPIO_BUTTON_PIN, GPIO.IN, pull_up_down=pull_resistor)
                     log.info(f"GPIO Button Pin ({GPIO_BUTTON_PIN}) setup as IN (Pull {'UP' if pull_resistor == GPIO.PUD_UP else 'DOWN'}).")
                 except Exception as e_setup: log.error(f"Failed to setup GPIO Button Pin ({GPIO_BUTTON_PIN}): {e_setup}"); setup_ok = False
            else: log.warning(f"Skipping Button Pin ({GPIO_BUTTON_PIN}) setup due to previous error.")
        else: log.info("GPIO Button Pin not configured (<= 0).")

        if setup_ok and (GPIO_LED_PIN > 0 or GPIO_BUTTON_PIN > 0):
             HAS_GPIO = True; log.info("GPIO initialized successfully for debug endpoints.")
        elif not (GPIO_LED_PIN > 0 or GPIO_BUTTON_PIN > 0):
             log.info("GPIO enabled, but no valid LED or Button pins configured for debug.")
             HAS_GPIO = False; GPIO = None
        else:
             log.warning("GPIO initialization failed during pin setup. Debug Button/LED disabled.")
             try: GPIO.cleanup()
             except Exception: pass
             HAS_GPIO = False; GPIO = None
    except ImportError: log.warning("RPi.GPIO library not found. Debug Button/LED disabled."); HAS_GPIO = False; GPIO = None
    except RuntimeError as e: log.error(f"GPIO Runtime Error during initialization: {e}. Debug Button/LED disabled."); HAS_GPIO = False; GPIO = None
    except Exception as e:
        log.exception(f"Unexpected error during GPIO initialization: {e}. Debug Button/LED disabled.")
        if GPIO:
            try: GPIO.cleanup()
            except Exception: pass
        HAS_GPIO = False; GPIO = None
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
# *** Store the fixed debug recording rate ***
_mic_rate_debug = 48000 # Fixed rate for debug recording

def _mic_worker_debug():
    """Dedicated thread for reading audio frames for debug recording."""
    global _frames, _stream_debug
    log.info(f"Debug Mic recording worker thread started (Target Rate: {_mic_rate_debug} Hz).")
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
        # Generate at the standard audio rate (44.1kHz)
        # Let play_audio handle resampling if needed (it now targets OUTPUT_SAMPLE_RATE)
        sine_wave = Sine(freq, sample_rate=44100) # Generate at 44.1k
        audio_segment = sine_wave.to_audio_segment(duration=dur_ms)
        audio_segment = audio_segment.set_sample_width(2) # 16-bit
        # Keep it mono, let play_audio convert to stereo if needed
        # audio_segment = audio_segment.set_channels(2)
        audio_segment.export(path, format="wav")
        log.info(f"Tone saved to {path} (Rate: 44100 Hz, Mono)")

        # Play using audio_manager (will resample to OUTPUT_SAMPLE_RATE & stereoize)
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
    if not (HAS_GPIO and GPIO and GPIO_LED_PIN > 0):
        reason = "Unknown GPIO issue"; # ... (error reason logic as before) ...
        if not GPIO: reason = "GPIO library object invalid"
        elif not HAS_GPIO: reason = "GPIO not available or initialization failed"
        elif GPIO_LED_PIN <= 0: reason = "LED pin not configured"
        log.warning(f"LED test endpoint called but GPIO is not ready: {reason}")
        return jsonify({"error": f"GPIO not ready: {reason}"}), 400

    def _blink():
        log.info(f"Blinking LED on pin {GPIO_LED_PIN}")
        try:
            # *** FIX: Set mode within the thread ***
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            # Pin should already be set up as OUT from initial setup if HAS_GPIO is True
            for _ in range(3):
                GPIO.output(GPIO_LED_PIN, GPIO.HIGH)
                time.sleep(0.25)
                GPIO.output(GPIO_LED_PIN, GPIO.LOW)
                time.sleep(0.25)
            log.info("LED blink finished.")
        except Exception as e:
             log.error(f"Error during LED blink thread: {e}")
             # Try to ensure LED is off even if error occurred
             try:
                 # Set mode again just in case error was mode related
                 GPIO.setmode(GPIO.BCM)
                 GPIO.setwarnings(False)
                 GPIO.output(GPIO_LED_PIN, GPIO.LOW)
             except Exception: pass

    threading.Thread(target=_blink, daemon=True).start()
    return jsonify({"status": "LED blink sequence started"})

@bp.route("/button", methods=["GET"])
def button():
    """Read current button state. Requires valid button pin and GPIO enabled."""
    if not (HAS_GPIO and GPIO and GPIO_BUTTON_PIN > 0):
        reason = "Unknown GPIO issue"; # ... (error reason logic as before) ...
        if not GPIO: reason = "GPIO library object invalid"
        elif not HAS_GPIO: reason = "GPIO not available or initialization failed"
        elif GPIO_BUTTON_PIN <= 0: reason = "Button pin not configured"
        log.warning(f"Button read endpoint called but GPIO is not ready: {reason}")
        return jsonify({"error": f"GPIO not ready: {reason}"}), 400

    try:
        # *** FIX: Set mode within the route handler for safety ***
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        # Pin should already be set up as IN from initial setup
        raw = GPIO.input(GPIO_BUTTON_PIN)
        pressed = (raw == 1) if BUTTON_ACTIVE_HIGH else (raw == 0)
        log.debug(f"Button pin {GPIO_BUTTON_PIN} read: raw={raw}, pressed={pressed} (Active High: {BUTTON_ACTIVE_HIGH})")
        return jsonify({"pressed": pressed, "raw_value": raw})
    except Exception as e:
        log.error(f"Error reading button state: {e}")
        return jsonify({"error": f"Failed to read button: {e}"}), 500


@bp.route("/mic/start", methods=["POST"])
def mic_start():
    """Begin recording audio at fixed 48kHz until /mic/stop."""
    global _recording_thread, _frames, _pa_debug, _stream_debug, _mic_rate_debug

    if _recording_active.is_set():
        log.warning("Debug Mic start requested, but already recording.")
        return jsonify({"error": "Already recording"}), 400

    _frames = []
    # *** FIX: Use hardcoded 48kHz for debug recording rate ***
    _mic_rate_debug = 48000
    log.info(f"Attempting to start Debug Mic recording at fixed rate: {_mic_rate_debug} Hz")

    try:
        _pa_debug = pyaudio.PyAudio()
        # Optional: Check if device explicitly supports 48kHz?
        # try:
        #     device_info = _pa_debug.get_device_info_by_index(MIC_DEVICE_INDEX)
        #     supported = _pa_debug.is_format_supported(
        #         _mic_rate_debug,
        #         input_device=MIC_DEVICE_INDEX,
        #         input_channels=MIC_CHANNELS,
        #         input_format=pyaudio.paInt16
        #     )
        #     if supported:
        #         log.info(f" -> Mic device index {MIC_DEVICE_INDEX} reports support for {_mic_rate_debug} Hz.")
        #     else:
        #         log.warning(f" -> Mic device index {MIC_DEVICE_INDEX} does NOT report support for {_mic_rate_debug} Hz. Recording might fail or use a different rate.")
        # except Exception as e_check:
        #     log.warning(f"Could not check format support for mic device: {e_check}")

        _stream_debug = _pa_debug.open(
            format=pyaudio.paInt16, # Use 16-bit PCM
            channels=MIC_CHANNELS,
            rate=_mic_rate_debug,   # *** Use the fixed 48kHz rate ***
            input=True,
            frames_per_buffer=MIC_CHUNK,
            input_device_index=MIC_DEVICE_INDEX,
            stream_callback=None # Use blocking read in worker thread
        )
        _recording_active.set()
        _recording_thread = threading.Thread(target=_mic_worker_debug, daemon=True)
        _recording_thread.start()
        log.info(f"Debug Mic recording started successfully at {_mic_rate_debug} Hz.")
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
        # _mic_rate_debug remains 48000 but recording failed
        return jsonify({"error": f"Failed to start recording: {e}"}), 500


@bp.route("/mic/stop", methods=["POST"])
def mic_stop():
    """Stop recording, apply gain, save the 48kHz WAV, return URL."""
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

    # Close PyAudio resources first
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

    # Process captured frames
    if not _frames:
        log.warning("Recording stopped, but no frames were captured.")
        # _mic_rate_debug still holds 48000 but is irrelevant now
        return jsonify({"error": "No audio data recorded"}), 400

    # Ensure rate is valid (should always be 48000 now unless start failed badly)
    if _mic_rate_debug <= 0:
         log.error("Cannot save WAV file: Invalid microphone sample rate captured.")
         _frames = []; return jsonify({"error": "Internal error: Invalid sample rate for saving."}), 500

    audio_dir = os.path.join(os.getcwd(), "audio_files")
    os.makedirs(audio_dir, exist_ok=True)
    ts = int(time.time())
    fname = f"debug_mic_{ts}.wav"
    path = os.path.join(audio_dir, fname)

    try:
        # Combine frames
        raw_data = b"".join(_frames)
        _frames = [] # Clear global frames list

        # *** Apply Software Gain ***
        if DEBUG_MIC_GAIN_FACTOR != 1.0 and raw_data:
            log.info(f"Applying software gain (factor: {DEBUG_MIC_GAIN_FACTOR:.2f}) to debug recording...")
            try:
                # Convert bytes to numpy array (assuming 16-bit)
                samples = np.frombuffer(raw_data, dtype=np.int16)

                # Amplify (use float32 to avoid intermediate overflow)
                amplified_samples = samples.astype(np.float32) * DEBUG_MIC_GAIN_FACTOR

                # Clip to prevent exceeding int16 range
                np.clip(amplified_samples, -32768, 32767, out=amplified_samples)

                # Convert back to int16
                final_samples = amplified_samples.astype(np.int16)

                # Convert back to bytes
                processed_data = final_samples.tobytes()
                log.info("Gain applied successfully.")
            except Exception as e_gain:
                log.error(f"Error applying software gain: {e_gain}. Saving original data.")
                processed_data = raw_data # Fallback to original data on error
        else:
            log.info("Skipping software gain (factor is 1.0 or no data).")
            processed_data = raw_data

        # Save the processed data
        log.info(f"Saving debug recording to {path} (Rate: {_mic_rate_debug} Hz, Channels: {MIC_CHANNELS}, Width: 16-bit)")
        wf = wave.open(path, "wb")
        wf.setnchannels(MIC_CHANNELS)
        sample_width = pyaudio.PyAudio().get_sample_size(pyaudio.paInt16) # Should be 2
        wf.setsampwidth(sample_width)
        wf.setframerate(_mic_rate_debug) # Use the fixed 48kHz rate
        wf.writeframes(processed_data)   # Write the potentially amplified data
        wf.close()
        log.info(f"Debug recording saved successfully: {fname}")

        return jsonify({"status": "Recording stopped and saved", "audio_url": f"/audio_files/{fname}"})

    except Exception as e:
        log.exception("Failed to save debug microphone recording.")
        return jsonify({"error": f"Failed to save recording: {e}"}), 500
    finally:
        _frames = [] # Ensure frames are cleared even on save error