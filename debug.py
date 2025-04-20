#!/usr/bin/env python3
"""
Simplified Debug endpoints: tone, LED, button, mic start/stop.
Ensures correct sample rate metadata in saved WAV files.
"""

import os
import time
import threading
import logging
import wave
from flask import Blueprint, jsonify, request
from pydub import AudioSegment, exceptions as pydub_exceptions
from pydub.generators import Sine
import pyaudio

from config import (
    GPIO_LED_PIN, GPIO_BUTTON_PIN, BUTTON_ACTIVE_HIGH, ENABLE_GPIO,
    MIC_DEVICE_INDEX, MIC_SAMPLE_RATE, MIC_CHANNELS, MIC_CHUNK
)
from audio_manager import play_audio # Re-import for tone playback

log = logging.getLogger("debug") # Use specific logger

# GPIO Setup (Conditional)
HAS_GPIO = False
if ENABLE_GPIO:
    try:
        import RPi.GPIO as GPIO
        GPIO.setwarnings(False) # Suppress channel already in use warnings
        GPIO.setmode(GPIO.BCM)
        # Setup pins only if they are valid (e.g., > 0)
        if GPIO_LED_PIN > 0:
             GPIO.setup(GPIO_LED_PIN, GPIO.OUT, initial=GPIO.LOW)
        if GPIO_BUTTON_PIN > 0:
             pull_resistor = GPIO.PUD_UP if not BUTTON_ACTIVE_HIGH else GPIO.PUD_DOWN
             GPIO.setup(GPIO_BUTTON_PIN, GPIO.IN, pull_up_down=pull_resistor)
        HAS_GPIO = True
        log.info("GPIO initialized for debug.")
    except Exception as e:
        log.warning(f"GPIO initialization failed in debug: {e}. Debug Button/LED disabled.")
        HAS_GPIO = False # Ensure it's False on error
else:
    log.info("GPIO disabled by configuration or platform.")


bp = Blueprint("debug", __name__, url_prefix="/api/debug")

# Globals for mic recording state
_recording_thread = None
_recording_active = threading.Event() # Use Event for clearer signaling
_frames = []
_pa_debug = None # Use separate instance for debug mic? Might avoid conflicts.
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
             if "Input overflowed" in str(e):
                  log.warning("Debug Mic input overflow detected.")
             else:
                  log.error(f"Debug Mic read OS error in worker: {e}")
                  _recording_active.clear() # Signal stop on error
                  break
        except Exception as e:
            log.exception("Unexpected error in debug mic recording worker.")
            _recording_active.clear() # Signal stop on error
            break

    # Append collected frames to global list *after* loop finishes or is stopped
    # This might be slightly safer if multiple threads access _frames, though unlikely here.
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
        sine_wave = Sine(freq)
        audio_segment = sine_wave.to_audio_segment(duration=dur_ms)
        # Export as WAV (ensure 16-bit for wider compatibility)
        audio_segment = audio_segment.set_sample_width(2)
        audio_segment.export(path, format="wav")
        log.info(f"Tone saved to {path}")

        # Play the generated tone using audio_manager
        log.info("Playing generated tone via audio_manager...")
        # This runs in the main Flask thread, blocking the request until done
        # Consider running in a separate thread if immediate response is needed
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
        log.info(f"Blinking LED on pin {GPIO_LED_PIN}")
        try:
            # Store original state if needed, but usually we force LOW at end
            for _ in range(3):
                GPIO.output(GPIO_LED_PIN, GPIO.HIGH)
                time.sleep(0.25)
                GPIO.output(GPIO_LED_PIN, GPIO.LOW)
                time.sleep(0.25)
            log.info("LED blink finished.")
        except Exception as e:
             log.error(f"Error during LED blink thread: {e}")
             # Attempt to ensure LED is off on error
             try: GPIO.output(GPIO_LED_PIN, GPIO.LOW)
             except Exception: pass

    threading.Thread(target=_blink, daemon=True).start()
    return jsonify({"status": "LED blink sequence started"})

@bp.route("/button", methods=["GET"])
def button():
    """Read current button state. Requires valid button pin and GPIO enabled."""
    if not HAS_GPIO or GPIO_BUTTON_PIN <= 0:
        return jsonify({"error": "GPIO not available or Button pin not configured"}), 400
    try:
        raw = GPIO.input(GPIO_BUTTON_PIN)
        # Logic based on config's BUTTON_ACTIVE_HIGH
        pressed = (raw == 1) if BUTTON_ACTIVE_HIGH else (raw == 0)
        log.debug(f"Button pin {GPIO_BUTTON_PIN} read: raw={raw}, pressed={pressed} (Active High={BUTTON_ACTIVE_HIGH})")
        return jsonify({"pressed": pressed})
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

    _frames = [] # Clear previous frames
    _mic_rate_debug = 0 # Reset rate

    try:
        _pa_debug = pyaudio.PyAudio()
        device_info = _pa_debug.get_device_info_by_index(MIC_DEVICE_INDEX)
        _mic_rate_debug = MIC_SAMPLE_RATE if MIC_SAMPLE_RATE > 0 else int(device_info["defaultSampleRate"])

        if _mic_rate_debug <= 0:
             raise ValueError(f"Invalid mic rate detected: {_mic_rate_debug}")

        log.info(f"Starting Debug Mic recording: Index={MIC_DEVICE_INDEX}, Rate={_mic_rate_debug} Hz, Channels={MIC_CHANNELS}")
        _stream_debug = _pa_debug.open(
            format=pyaudio.paInt16, # Use 16-bit PCM
            channels=MIC_CHANNELS,
            rate=_mic_rate_debug,
            input=True,
            frames_per_buffer=MIC_CHUNK,
            input_device_index=MIC_DEVICE_INDEX,
            stream_callback=None # Use blocking read in worker thread
        )
        # Don't necessarily need start_stream() with blocking read, but ensures it's ready
        _stream_debug.start_stream()

        _recording_active.set() # Signal worker to start

        # Start the dedicated worker thread
        _recording_thread = threading.Thread(target=_mic_worker_debug, daemon=True)
        _recording_thread.start()

        log.info("Debug Mic recording started successfully.")
        return jsonify({"status": "Recording started"})

    except Exception as e:
        log.exception("Failed to start Debug Mic recording.")
        _recording_active.clear()
        # Cleanup partial resources
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
        # If already stopped but called again, maybe just return status?
        # For now, treat as error if not actively recording.
        log.warning("Debug Mic stop requested, but not recording.")
        return jsonify({"error": "Not recording"}), 400

    log.info("Stopping Debug Mic recording...")
    _recording_active.clear() # Signal worker thread to stop

    # Wait for the worker thread to finish
    if _recording_thread and _recording_thread.is_alive():
        log.debug("Waiting for debug recording thread to finish...")
        _recording_thread.join(timeout=1.5) # Slightly longer timeout
        if _recording_thread.is_alive():
             log.warning("Debug recording thread did not finish cleanly.")
        _recording_thread = None

    # Close and terminate PyAudio resources (use the _debug versions)
    if _stream_debug:
        try:
            # Check if active before stopping
            if _stream_debug.is_active():
                 _stream_debug.stop_stream()
            _stream_debug.close()
            log.debug("Debug Mic stream closed.")
        except Exception as e:
            log.error(f"Error closing debug mic stream: {e}")
        finally:
             _stream_debug = None

    if _pa_debug:
        try:
            _pa_debug.terminate()
            log.debug("Debug PyAudio instance terminated.")
        except Exception as e:
            log.error(f"Error terminating debug PyAudio: {e}")
        finally:
            _pa_debug = None

    # --- Save the recorded frames ---
    # Check frames *after* resource cleanup
    if not _frames:
        log.warning("Recording stopped, but no frames were captured.")
        # Reset rate even if no frames
        _mic_rate_debug = 0
        return jsonify({"error": "No audio data recorded"}), 400

    # Check if rate is valid before saving
    if _mic_rate_debug <= 0:
         log.error("Cannot save WAV file: Invalid microphone sample rate captured during start.")
         _frames = [] # Clear frames
         return jsonify({"error": "Internal error: Invalid sample rate for saving."}), 500

    audio_dir = os.path.join(os.getcwd(), "audio_files")
    os.makedirs(audio_dir, exist_ok=True)
    ts = int(time.time())
    fname = f"debug_mic_{ts}.wav"
    path = os.path.join(audio_dir, fname)

    try:
        log.info(f"Saving debug recording to {path} (Rate: {_mic_rate_debug} Hz, Channels: {MIC_CHANNELS}, Width: 16-bit)")
        wf = wave.open(path, "wb")
        wf.setnchannels(MIC_CHANNELS)
        # Get sample width corresponding to paInt16 (2 bytes)
        sample_width = pyaudio.PyAudio().get_sample_size(pyaudio.paInt16)
        wf.setsampwidth(sample_width)
        wf.setframerate(_mic_rate_debug) # Use the **actual recording rate**
        wf.writeframes(b"".join(_frames))
        wf.close()
        log.info(f"Debug recording saved successfully: {fname}")

        return jsonify({"status": "Recording stopped and saved", "audio_url": f"/audio_files/{fname}"})

    except Exception as e:
        log.exception("Failed to save debug microphone recording.")
        return jsonify({"error": f"Failed to save recording: {e}"}), 500
    finally:
        # Clear frames and reset rate regardless of save success/failure
        _frames = []
        _mic_rate_debug = 0