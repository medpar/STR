# ================================================
# File: /app.py
# ================================================
# app.py

#!/usr/bin/env python3
"""
Flask / Flask‑SocketIO server for STR, now with OpenAI Realtime API,
PDF concepts & questions, chat UI, and integrated GPIO control.
"""

from __future__ import annotations
import os
import logging
import threading
import atexit
import queue
import wave # Needed for saving temporary audio

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory
from flask_socketio import SocketIO, emit
from elevenlabs import ElevenLabs
from openai import OpenAI # Still needed for non-realtime parts

from agents import process_query
from tts import generate_tts
# Import the NEW RealtimeClient and helper function
from realtime import RealtimeClient, save_temp_wav, INSTRUCTIONS, TEMP_AUDIO_DIR
from files import PDFManager
from config import (
    MIC_DEVICE_INDEX,
    VECTOR_STORE_ID,
    ENABLE_GPIO, # Check if GPIO is enabled
    # Get voice ID for realtime if configured separately, else use TTS voice?
    # Let's assume a specific realtime voice or fallback to TTS voice
)
from debug import bp as debug_bp
from gpio_controller import GPIOController # Import GPIO controller
# Import audio_manager functions needed
from audio_manager import play_audio, terminate_pyaudio_instance as terminate_audio_manager_pyaudio

# --------------------------------------------------
# Basic Setup
# --------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)5s | %(name)s | %(message)s",
)
log = logging.getLogger("app") # App specific logger
load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "audio_files")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(TEMP_AUDIO_DIR, exist_ok=True) # Ensure temp dir exists

# --------------------------------------------------
# API Keys & Config
# --------------------------------------------------
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
TTS_VOICE_ID = os.getenv("VOICE_ID") # Voice for ElevenLabs TTS
MODEL_ID = os.getenv("MODEL_ID") # Model for ElevenLabs TTS

# --- Realtime Config ---
# Use a specific env var for realtime voice, or fallback to TTS voice ID
REALTIME_VOICE = os.getenv("REALTIME_VOICE_ID", "ash") # Default 'ash' or use TTS_VOICE_ID as fallback?
log.info(f"Using Realtime Voice: {REALTIME_VOICE}")

if not all([ELEVENLABS_API_KEY, TTS_VOICE_ID, MODEL_ID]):
    logging.warning("Missing ElevenLabs API Key, Voice ID, or Model ID. TTS features might be limited.")

# Check for OpenAI Key (needed for Realtime and Agent)
if not os.getenv("OPENAI_API_KEY"):
     log.critical("FATAL: OPENAI_API_KEY environment variable not set. Realtime and Agent features will fail.")
     # Optionally exit or disable features explicitly
     # exit(1)


# --------------------------------------------------
# Flask App and Extensions Initialization
# --------------------------------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'default-secret-key-please-change')
# Use simple_websocket for better compatibility if needed, otherwise default should be okay
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading') # Use threading async_mode

app.register_blueprint(debug_bp)

# --------------------------------------------------
# Service Clients Initialization
# --------------------------------------------------
try:
    eleven_client = ElevenLabs(api_key=ELEVENLABS_API_KEY) if ELEVENLABS_API_KEY else None
except Exception as e:
    logging.error("Failed to initialize ElevenLabs client: %s", e)
    eleven_client = None

try:
    # Standard OpenAI client for Agent/PDF features
    openai_client = OpenAI()
except Exception as e:
    logging.error("Failed to initialize standard OpenAI client: %s", e)
    # Decide if this is fatal or just disables some features
    openai_client = None # Allow app to run but agent/pdf might fail

pdf_manager = PDFManager(UPLOAD_DIR, VECTOR_STORE_ID) if openai_client and VECTOR_STORE_ID else None
if not pdf_manager:
    log.warning("PDF Manager disabled (OpenAI client init failed or VECTOR_STORE_ID missing).")

# --------------------------------------------------
# Realtime Client Setup
# --------------------------------------------------
realtime_client = None
gpio_controller = None
_realtime_audio_buffer = bytearray() # Buffer for incoming audio chunks
_realtime_audio_lock = threading.Lock()

# --- Realtime Callbacks ---
def _on_text_delta(delta: str):
    """Callback for text chunks from RealtimeClient."""
    # log.debug(f"Broadcasting text delta: {delta}")
    socketio.emit("text_delta", {"delta": delta}, namespace="/realtime")

def _on_audio_chunk(chunk: bytes):
    """Callback for audio chunks from RealtimeClient."""
    global _realtime_audio_buffer
    with _realtime_audio_lock:
        _realtime_audio_buffer.extend(chunk)
        # log.debug(f"Audio chunk received, buffer size: {len(_realtime_audio_buffer)}")

def _on_response_done():
    """Callback when an audio response is complete."""
    global _realtime_audio_buffer
    log.info("Realtime response done callback triggered.")
    filepath = None
    temp_audio_data = None

    with _realtime_audio_lock:
        if _realtime_audio_buffer:
            # Copy data before saving/clearing
            temp_audio_data = bytes(_realtime_audio_buffer)
            _realtime_audio_buffer.clear() # Clear buffer immediately after copying
            log.info(f"Audio buffer copied ({len(temp_audio_data)} bytes) and cleared.")
        else:
            log.warning("Response done, but audio buffer is empty.")

    if temp_audio_data:
        log.info("Attempting to save and play received audio...")
        # Save the buffered audio to a temporary WAV file
        filepath = save_temp_wav(temp_audio_data)
        if filepath:
            try:
                # Play the saved WAV using audio_manager
                play_audio(filepath)
                log.info(f"Playback initiated for temporary file: {filepath}")
                # Optionally delete the temp file immediately after starting playback?
                # Or schedule deletion? For now, keep it.
            except Exception as e:
                log.exception(f"Error playing temporary audio file {filepath}: {e}")
        else:
            log.error("Failed to save temporary audio data, cannot play.")

def _on_status_update(message: str):
    """Callback for status updates from RealtimeClient."""
    log.info(f"Realtime Status Update: {message}")
    socketio.emit("status", {"message": message}, namespace="/realtime")

def _on_user_transcription(text: str):
    """Callback for user transcriptions from RealtimeClient."""
    log.info(f"User transcription received: {text}")
    socketio.emit("user_transcription", {"text": text}, namespace="/realtime")

# --- Initialize RealtimeClient ---
try:
    realtime_client = RealtimeClient(
        instructions=INSTRUCTIONS,
        voice=REALTIME_VOICE,
        mic_index=MIC_DEVICE_INDEX,
        on_text_delta=_on_text_delta,
        on_audio_chunk=_on_audio_chunk,
        on_response_done=_on_response_done,
        on_status_update=_on_status_update,
        on_user_transcription=_on_user_transcription,
    )
    # Start the client's background asyncio loop
    realtime_client.start_background_loop()
    log.info("RealtimeClient initialized and background loop started.")

except ValueError as e: # Catch API key error specifically
     log.critical(f"Failed to initialize RealtimeClient: {e}")
     realtime_client = None
except Exception as e:
    log.exception("Failed to initialize RealtimeClient:")
    realtime_client = None

# --- Initialize GPIO Controller (after RealtimeClient) ---
if ENABLE_GPIO and realtime_client:
    try:
        # Pass the client's methods directly as callbacks
        gpio_controller = GPIOController(
            start_cb=realtime_client.start_talking,
            stop_cb=realtime_client.stop_talking
        )
        if not gpio_controller.available:
             log.warning("GPIOController initialized but is not available/active.")
             gpio_controller = None # Ensure it's None if not active
        else:
             log.info("GPIOController initialized successfully.")
    except Exception as e:
        log.exception("Failed to initialize GPIOController:")
        gpio_controller = None
elif ENABLE_GPIO and not realtime_client:
     log.warning("GPIO is enabled in config, but RealtimeClient failed to initialize. GPIOController not started.")
else:
    log.info("GPIO is disabled by configuration or platform. GPIOController not started.")


# --------------------------------------------------
# Cleanup Function
# --------------------------------------------------
@atexit.register
def cleanup_app():
    log.info("Application exiting. Cleaning up resources...")
    if gpio_controller:
        log.info("Cleaning up GPIOController...")
        gpio_controller.cleanup()
    if realtime_client:
        log.info("Stopping RealtimeClient background loop...")
        realtime_client.stop_background_loop() # This handles internal cleanup
    # Terminate audio manager's PyAudio instance if it was used
    terminate_audio_manager_pyaudio()
    log.info("Cleanup complete.")


# --------------------------------------------------
# Basic Routes
# --------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/audio_files/<path:filename>")
def serve_audio(filename):
    # Security: Prevent accessing files outside AUDIO_DIR (e.g., ../../.. )
    safe_path = os.path.abspath(os.path.join(AUDIO_DIR, filename))
    if not safe_path.startswith(os.path.abspath(AUDIO_DIR)):
        return "Forbidden", 403
    return send_from_directory(AUDIO_DIR, filename)

# Add route for temporary realtime audio if needed for debugging, but generally not required
# @app.route("/audio_files/temp_realtime/<path:filename>")
# def serve_temp_realtime_audio(filename):
#     safe_path = os.path.abspath(os.path.join(TEMP_AUDIO_DIR, filename))
#     if not safe_path.startswith(os.path.abspath(TEMP_AUDIO_DIR)):
#         return "Forbidden", 403
#     return send_from_directory(TEMP_AUDIO_DIR, filename)

# --------------------------------------------------
# TTS Endpoint
# --------------------------------------------------
@app.route("/api/tts", methods=["POST"])
def tts_endpoint():
    if not eleven_client:
        return jsonify({"error": "TTS service (ElevenLabs) not available"}), 503

    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400
    try:
        # Use TTS_VOICE_ID for ElevenLabs
        res = generate_tts(text, eleven_client, TTS_VOICE_ID, MODEL_ID, AUDIO_DIR)
        safe_filename = os.path.basename(res.get('mp3_filename', ''))
        if not safe_filename:
            raise ValueError("Invalid filename received from TTS generation")
        audio_url = f"/audio_files/{safe_filename}"
        return jsonify({"message": res.get("message", text), "audio_url": audio_url})
    except Exception as e:
        logging.exception("TTS generation failed")
        return jsonify({"error": f"TTS generation failed: {e}"}), 500

# --------------------------------------------------
# GPT Agent Endpoint
# --------------------------------------------------
@app.route("/api/agent", methods=["POST"])
def agent_endpoint():
    if not openai_client:
         return jsonify({"error": "Agent service (OpenAI) not available"}), 503

    query = (request.get_json(silent=True) or {}).get("query", "").strip()
    if not query:
        return jsonify({"error": "No query provided"}), 400

    try:
        answer = process_query(query)
    except Exception as e:
        logging.exception("Agent query processing failed")
        return jsonify({"error": f"Agent processing failed: {e}"}), 500

    audio_url = None
    # Generate TTS for the agent response if ElevenLabs is available
    if eleven_client:
        try:
            # Use TTS_VOICE_ID for ElevenLabs
            res = generate_tts(answer, eleven_client, TTS_VOICE_ID, MODEL_ID, AUDIO_DIR)
            safe_filename = os.path.basename(res.get('mp3_filename', ''))
            if safe_filename:
                audio_url = f"/audio_files/{safe_filename}"
        except Exception:
            logging.exception("TTS generation failed for agent response")

    return jsonify({"message": answer, "audio_url": audio_url})

# --------------------------------------------------
# PDF Endpoints – upload & ask & concepts & questions
# --------------------------------------------------
@app.route("/api/pdf/upload", methods=["POST"])
def pdf_upload():
    if not pdf_manager:
         return jsonify({"error": "PDF service not available"}), 503
    if 'file' not in request.files:
        return jsonify({"error": "No file part in the request"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Invalid file type, only PDF allowed"}), 400

    try:
        info = pdf_manager.upload(file)
        return jsonify(info)
    except ValueError as ve:
        logging.warning("PDF upload validation error: %s", ve)
        return jsonify({"error": str(ve)}), 400
    except Exception as exc:
        logging.exception("PDF upload failed")
        return jsonify({"error": f"An unexpected error occurred during upload: {exc}"}), 500

@app.route("/api/pdf/ask", methods=["POST"])
def pdf_ask():
    if not pdf_manager:
         return jsonify({"error": "PDF service not available"}), 503
    data = request.get_json(silent=True) or {}
    question = data.get("question", "").strip()
    filename = data.get("filename") or pdf_manager.get_current_filename()
    if not filename:
        return jsonify({"error": "No PDF file specified or previously uploaded in this session."}), 400
    if not question:
        return jsonify({"error": "No question provided"}), 400

    try:
        answer = pdf_manager.ask(filename, question)
        audio_url = None
        # Generate TTS for the answer if ElevenLabs is available
        if eleven_client:
            try:
                # Use TTS_VOICE_ID for ElevenLabs
                res = generate_tts(answer, eleven_client, TTS_VOICE_ID, MODEL_ID, AUDIO_DIR)
                safe_filename = os.path.basename(res.get('mp3_filename', ''))
                if safe_filename:
                    audio_url = f"/audio_files/{safe_filename}"
            except Exception:
                logging.exception("TTS generation failed for PDF answer")
        return jsonify({"answer": answer, "audio_url": audio_url})
    except ValueError as ve:
        logging.warning("PDF ask validation error: %s", ve)
        return jsonify({"error": str(ve)}), 400
    except Exception as exc:
        logging.exception("PDF ask error")
        return jsonify({"error": f"An unexpected error occurred while asking: {exc}"}), 500

@app.route("/api/pdf/concepts", methods=["POST"])
def pdf_concepts():
    if not pdf_manager:
         return jsonify({"error": "PDF service not available"}), 503
    data = request.get_json(silent=True) or {}
    filename = data.get("filename") or pdf_manager.get_current_filename()
    if not filename:
        return jsonify({"error":"No PDF file specified or active."}), 400

    try:
        concepts = pdf_manager.concepts(filename)
        return jsonify({"concepts": concepts})
    except ValueError as ve:
        return jsonify({"error":str(ve)}), 400
    except Exception as e:
        logging.exception("PDF concepts error")
        return jsonify({"error":f"Failed to get concepts: {e}"}), 500

@app.route("/api/pdf/questions", methods=["POST"])
def pdf_questions():
    if not pdf_manager:
         return jsonify({"error": "PDF service not available"}), 503
    data = request.get_json(silent=True) or {}
    filename = data.get("filename") or pdf_manager.get_current_filename()
    if not filename:
        return jsonify({"error":"No PDF file specified or active."}), 400

    try:
        questions = pdf_manager.questions(filename)
        return jsonify({"questions": questions})
    except ValueError as ve:
        return jsonify({"error":str(ve)}), 400
    except Exception as e:
        logging.exception("PDF questions error")
        return jsonify({"error":f"Failed to generate questions: {e}"}), 500

# --------------------------------------------------
# SocketIO – Realtime
# --------------------------------------------------
@socketio.on("connect", namespace="/realtime")
def rt_connect():
    client_sid = request.sid
    log.info(f"Client connected to /realtime namespace (sid: {client_sid})")
    if realtime_client and realtime_client._connected.is_set():
         # Optionally send the *very first* message from instructions on connect
         # For now, just send Ready status
         _on_status_update("Ready.") # Send initial status via callback
    elif realtime_client:
         _on_status_update("Connecting...") # Send current status if client exists but not connected
    else:
         emit("status", {"message": "Realtime Error: Service unavailable"})

@socketio.on("disconnect", namespace="/realtime")
def rt_disconnect():
    client_sid = request.sid
    log.info(f"Client disconnected from /realtime namespace (sid: {client_sid})")

@socketio.on("start_talking", namespace="/realtime")
def rt_start():
    client_sid = request.sid
    if realtime_client:
        log.info(f"rt_start: Requesting start talking (sid: {client_sid})")
        # Clear previous audio buffer before starting new recording
        global _realtime_audio_buffer
        with _realtime_audio_lock:
             _realtime_audio_buffer.clear()
        realtime_client.start_talking() # This will trigger status updates via callback
    else:
        emit("status", {"message": "Realtime Error: Service unavailable"})

@socketio.on("stop_talking", namespace="/realtime")
def rt_stop():
    client_sid = request.sid
    if realtime_client:
        log.info(f"rt_stop: Requesting stop talking (sid: {client_sid})")
        realtime_client.stop_talking() # This will trigger status updates via callback
    else:
        emit("status", {"message": "Realtime Error: Service unavailable"})

@socketio.on("send_text", namespace="/realtime")
def rt_send_text(data):
    client_sid = request.sid
    text = (data or {}).get("text", "").strip()
    if text and realtime_client:
        log.info(f"rt_send_text: Sending text: '{text[:50]}...' (sid: {client_sid})")
         # Clear previous audio buffer when sending text too? Maybe not necessary.
        realtime_client.send_text(text) # This will trigger status updates via callback
    elif not realtime_client:
        emit("status", {"message": "Realtime Error: Service unavailable"})
    elif not text:
        log.warning(f"rt_send_text: Empty text received (sid: {client_sid})")


# --------------------------------------------------
# Main Execution
# --------------------------------------------------
if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_PORT", "5000"))
    debug_env = os.getenv("FLASK_DEBUG", "False").lower()
    run_in_debug_mode = debug_env in ["true", "1", "yes"]

    # Disable Flask's default logger if using basicConfig or custom logging
    # app.logger.disabled = True
    # log = logging.getLogger('werkzeug')
    # log.setLevel(logging.INFO) # Or WARNING to reduce noise

    log.info(f"Starting Flask app with SocketIO on {host}:{port} (Debug: {run_in_debug_mode})")
    # use_reloader=False is important when running background threads like the realtime client
    # Debug mode with reloader can cause issues with threads and cleanup.
    socketio.run(app, host=host, port=port, debug=run_in_debug_mode, use_reloader=False, allow_unsafe_werkzeug=run_in_debug_mode)