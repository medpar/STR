#!/usr/bin/env python3
"""
Simplified Debug endpoints: tone, LED, button, mic start/stop.
"""

import os, time, threading, logging, wave
from flask import Blueprint, jsonify, request # Added request back
from pydub.generators import Sine
import pyaudio

from config import (
    GPIO_LED_PIN, GPIO_BUTTON_PIN, BUTTON_ACTIVE_HIGH, ENABLE_GPIO, # Added ENABLE_GPIO
    MIC_DEVICE_INDEX, MIC_SAMPLE_RATE, MIC_CHANNELS, MIC_CHUNK
)
# Removed audio_manager import here, play_audio call removed from mic_stop

log = logging.getLogger("debug") # Use specific logger

# GPIO Setup (Conditional)
HAS_GPIO = False
if ENABLE_GPIO:
    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        # Setup pins only if they are valid (e.g., > 0)
        if GPIO_LED_PIN > 0:
             GPIO.setup(GPIO_LED_PIN, GPIO.OUT, initial=GPIO.LOW)
        if GPIO_BUTTON_PIN > 0:
             # Determine pull resistor based on active high/low
             pull_resistor = GPIO.PUD_UP if not BUTTON_ACTIVE_HIGH else GPIO.PUD_DOWN
             GPIO.setup(GPIO_BUTTON_PIN, GPIO.IN, pull_up_down=pull_resistor)
        HAS_GPIO = True
        log.info("GPIO initialized for debug.")
    except Exception as e:
        # Catch specific ImportErrors or RuntimeErrors if needed, or general Exception
        log.warning(f"GPIO initialization failed in debug: {e}. Debug Button/LED disabled.")
        HAS_GPIO = False # Ensure it's False on error

bp = Blueprint("debug", __name__, url_prefix="/api/debug")

# Globals for mic recording state
_recording_thread = None
_recording_active = threading.Event() # Use Event for clearer signaling
_frames = []
_pa = None
_stream = None
_mic_rate = 0 # Store sample rate used for recording

def _mic_worker():
    """Dedicated thread for reading audio frames."""
    global _frames, _stream
    log.info("Mic recording worker thread started.")
    while _recording_active.is_set() and _stream and _stream.is_active():
        try:
            data = _stream.read(MIC_CHUNK, exception_on_overflow=False)
            _frames.append(data)
        except OSError as e:
            # Handle specific recoverable errors like overflow differently if needed
             if "Input overflowed" in str(e):
                  log.warning("Mic input overflow detected in debug worker.")
                  # Optionally skip frame or short sleep: time.sleep(0.01)
             else:
                  log.error(f"Mic read OS error in debug worker: {e}")
                  _recording_active.clear() # Signal stop on error
                  break # Exit loop on significant error
        except Exception as e:
            log.exception("Unexpected error in mic recording worker.")
            _recording_active.clear() # Signal stop on error
            break # Exit loop
    log.info("Mic recording worker thread finished.")

@bp.route("/tone", methods=["POST"])
def tone():
    """
    Generate and save a sine tone WAV file. Returns the URL.
    Query args: frequency (Hz), duration (ms)
    """
    # Need to re-import play_audio here if we want to play it from backend
    from audio_manager import play_audio
    freq = float(request.args.get("frequency", 440))
    # Expect duration in milliseconds for pydub
    dur_ms = int(request.args.get("duration", 500)) # Default 500ms
    # Create audio_files directory if it doesn't exist
    audio_dir = os.path.join(os.getcwd(), "audio_files")
    os.makedirs(audio_dir, exist_ok=True)

    fname = f"debug_tone_{int(freq)}Hz_{dur_ms}ms.wav"
    path = os.path.join(audio_dir, fname)

    try:
        log.info(f"Generating tone: {freq} Hz, {dur_ms} ms")
        # Generate tone using pydub
        sine_wave = Sine(freq)
        # Duration is in milliseconds
        audio_segment = sine_wave.to_audio_segment(duration=dur_ms)
        # Export as WAV
        audio_segment.export(path, format="wav")
        log.info(f"Tone saved to {path}")

        # --- Play the generated tone ---
        # Since play_audio runs in the main thread (or spawns its own),
        # this POST request will wait until playback finishes.
        log.info("Playing generated tone...")
        play_audio(path) # Call the playback function
        log.info("Finished playing tone.")
        # --- ---

        return jsonify({"status": "Tone generated and played", "audio_url": f"/audio_files/{fname}"})
    except Exception as e:
        log.exception("Error generating or playing tone.")
        return jsonify({"error": f"Failed to process tone: {e}"}), 500


@bp.route("/led", methods=["POST"])
def led():
    """Blink the LED 3× at 0.5s intervals. Requires valid LED pin."""
    if not HAS_GPIO or GPIO_LED_PIN <= 0:
        return jsonify({"error": "GPIO not available or LED pin not configured"}), 400

    def _blink():
        log.info(f"Blinking LED on pin {GPIO_LED_PIN}")
        original_state = GPIO.input(GPIO_LED_PIN)
        try:
            for _ in range(3):
                GPIO.output(GPIO_LED_PIN, GPIO.HIGH)
                time.sleep(0.25) # Faster blink
                GPIO.output(GPIO_LED_PIN, GPIO.LOW)
                time.sleep(0.25)
            # Optional: restore original state, though usually we want it LOW
            GPIO.output(GPIO_LED_PIN, GPIO.LOW)
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
    """Read current button state. Requires valid button pin."""
    if not HAS_GPIO or GPIO_BUTTON_PIN <= 0:
        return jsonify({"error": "GPIO not available or Button pin not configured"}), 400
    try:
        raw = GPIO.input(GPIO_BUTTON_PIN)
        # Logic depends on whether button connects to GND (active low) or 3.3V (active high)
        # and the pull resistor used in setup.
        # If Active Low (connects to GND) & PUD_UP: raw is 0 when pressed.
        # If Active High (connects to 3.3V) & PUD_DOWN: raw is 1 when pressed.
        pressed = (raw == 0) if not BUTTON_ACTIVE_HIGH else (raw == 1)
        log.debug(f"Button pin {GPIO_BUTTON_PIN} read: raw={raw}, pressed={pressed}")
        return jsonify({"pressed": pressed})
    except Exception as e:
        log.error(f"Error reading button state: {e}")
        return jsonify({"error": f"Failed to read button: {e}"}), 500


@bp.route("/mic/start", methods=["POST"])
def mic_start():
    """Begin recording audio indefinitely until /mic/stop is called."""
    global _recording_thread, _frames, _pa, _stream, _mic_rate

    if _recording_active.is_set():
        log.warning("Mic start requested, but already recording.")
        return jsonify({"error": "Already recording"}), 400

    _frames = [] # Clear previous frames
    _recording_active.set() # Signal worker to start/continue

    try:
        _pa = pyaudio.PyAudio()
        # Determine sample rate: Use config if set, else device default
        device_info = _pa.get_device_info_by_index(MIC_DEVICE_INDEX)
        _mic_rate = MIC_SAMPLE_RATE if MIC_SAMPLE_RATE > 0 else int(device_info["defaultSampleRate"])

        log.info(f"Starting mic recording: Index={MIC_DEVICE_INDEX}, Rate={_mic_rate}, Channels={MIC_CHANNELS}")
        _stream = _pa.open(
            format=pyaudio.paInt16,
            channels=MIC_CHANNELS,
            rate=_mic_rate,
            input=True,
            frames_per_buffer=MIC_CHUNK,
            input_device_index=MIC_DEVICE_INDEX,
            stream_callback=None # Using blocking read in worker thread
        )
        _stream.start_stream() # Ensure stream is active

        # Start the dedicated worker thread
        _recording_thread = threading.Thread(target=_mic_worker, daemon=True)
        _recording_thread.start()

        log.info("Mic recording started successfully.")
        return jsonify({"status": "Recording started"})

    except Exception as e:
        log.exception("Failed to start microphone recording.")
        _recording_active.clear() # Ensure flag is cleared on error
        # Cleanup partial resources if necessary
        if _stream:
            try:
                 if _stream.is_active(): _stream.stop_stream()
                 _stream.close()
            except Exception: pass
            _stream = None
        if _pa:
            try: _pa.terminate()
            except Exception: pass
            _pa = None
        return jsonify({"error": f"Failed to start recording: {e}"}), 500


@bp.route("/mic/stop", methods=["POST"])
def mic_stop():
    """Stop recording, save the WAV file, and return its URL."""
    global _recording_thread, _frames, _pa, _stream, _mic_rate

    if not _recording_active.is_set():
        log.warning("Mic stop requested, but not recording.")
        return jsonify({"error": "Not recording"}), 400

    log.info("Stopping mic recording...")
    _recording_active.clear() # Signal worker thread to stop

    # Wait for the worker thread to finish processing the last chunks
    if _recording_thread and _recording_thread.is_alive():
        log.debug("Waiting for recording thread to finish...")
        _recording_thread.join(timeout=1.0) # Wait up to 1 second
        if _recording_thread.is_alive():
             log.warning("Recording thread did not finish cleanly.")
        _recording_thread = None

    # Close and terminate PyAudio resources
    if _stream:
        try:
            if _stream.is_active():
                 _stream.stop_stream()
            _stream.close()
            log.debug("Mic stream closed.")
        except Exception as e:
            log.error(f"Error closing mic stream: {e}")
        finally:
             _stream = None # Ensure it's cleared

    if _pa:
        try:
            _pa.terminate()
            log.debug("PyAudio instance terminated.")
        except Exception as e:
            log.error(f"Error terminating PyAudio: {e}")
        finally:
            _pa = None # Ensure it's cleared

    if not _frames:
        log.warning("Recording stopped, but no frames were captured.")
        return jsonify({"error": "No audio data recorded"}), 400

    # Save the recorded frames to a WAV file
    audio_dir = os.path.join(os.getcwd(), "audio_files")
    os.makedirs(audio_dir, exist_ok=True)
    ts = int(time.time())
    fname = f"debug_mic_{ts}.wav"
    path = os.path.join(audio_dir, fname)

    try:
        log.info(f"Saving recording to {path} (Rate: {_mic_rate}, Channels: {MIC_CHANNELS})")
        wf = wave.open(path, "wb")
        wf.setnchannels(MIC_CHANNELS)
        # Use sample width from PyAudio format paInt16 (which is 2 bytes)
        wf.setsampwidth(pyaudio.PyAudio().get_sample_size(pyaudio.paInt16))
        wf.setframerate(_mic_rate if _mic_rate > 0 else 44100) # Use recorded rate, fallback if somehow 0
        wf.writeframes(b"".join(_frames))
        wf.close()
        log.info(f"Recording saved successfully: {fname}")
        # Clear frames now that they are saved
        _frames = []
        # DO NOT PLAY AUDIO HERE - Just return URL
        return jsonify({"status": "Recording stopped and saved", "audio_url": f"/audio_files/{fname}"})

    except Exception as e:
        log.exception("Failed to save microphone recording.")
        _frames = [] # Clear frames even on error
        return jsonify({"error": f"Failed to save recording: {e}"}), 500