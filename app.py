# ================================================
# File: /app.py
# ================================================
#!/usr/bin/env python3
"""
Flask / Flask‑SocketIO server for STR, now with PDF concepts & questions,
chat UI, and Bluetooth control.
"""

from __future__ import annotations
import os
import logging
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory
from flask_socketio import SocketIO, emit
from elevenlabs import ElevenLabs
from openai import OpenAI

from agents import process_query
from tts import generate_tts
from realtime import RealtimeClient, INSTRUCTIONS
from files import PDFManager
from config import MIC_DEVICE_INDEX, VECTOR_STORE_ID # Removed duplicate MIC_DEVICE_INDEX
# Import Bluetooth functions
import bluetooth as bt # Renamed to avoid conflict with standard library

# ------------------------------------------------------------------
# Basic Setup
# ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)5s | %(name)s | %(message)s", # Added format
)
load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "audio_files")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ------------------------------------------------------------------
# API Keys & Config
# ------------------------------------------------------------------
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
VOICE_ID = os.getenv("VOICE_ID")
MODEL_ID = os.getenv("MODEL_ID")
if not all([ELEVENLABS_API_KEY, VOICE_ID, MODEL_ID]):
    # Use logging instead of raising immediately for potentially optional features
    logging.warning("Missing ElevenLabs API Key, Voice ID, or Model ID. TTS features might be limited.")
    # Or raise RuntimeError("Missing ELEVENLABS_API_KEY, VOICE_ID, MODEL_ID") if critical

# ------------------------------------------------------------------
# Flask App and Extensions Initialization
# ------------------------------------------------------------------
app = Flask(__name__)
# Secret key is needed for sessions, flash messages, etc. Good practice even if not used directly yet.
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'default-secret-key-please-change')
socketio = SocketIO(app, cors_allowed_origins="*")

# ------------------------------------------------------------------
# Service Clients Initialization
# ------------------------------------------------------------------
try:
    eleven_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)
except Exception as e:
    logging.error("Failed to initialize ElevenLabs client: %s", e)
    eleven_client = None # Allow app to run without ElevenLabs if init fails

try:
    openai_client = OpenAI() # Assumes OPENAI_API_KEY is set in env
except Exception as e:
    logging.error("Failed to initialize OpenAI client: %s", e)
    # Decide if this is critical
    raise RuntimeError(f"Failed to initialize OpenAI client: {e}") from e

pdf_manager = PDFManager(UPLOAD_DIR, VECTOR_STORE_ID)

# ------------------------------------------------------------------
# Realtime Client Setup
# ------------------------------------------------------------------
def _broadcast(msg: str):
    """Helper to broadcast messages via SocketIO."""
    logging.info("Broadcasting message: %s", msg)
    socketio.emit("broadcast_text", {"message": msg}, namespace="/realtime")

try:
    realtime_client = RealtimeClient(
        instructions=INSTRUCTIONS,
        voice="ash", # Consider making this configurable
        mic_index=MIC_DEVICE_INDEX,
        on_text=_broadcast,
    )
except Exception as e:
    logging.error("Failed to initialize RealtimeClient: %s", e)
    # Decide how to handle this - maybe disable realtime tab?
    realtime_client = None # Allow app to run but realtime might fail


# ------------------------------------------------------------------
# Basic Routes
# ------------------------------------------------------------------
@app.route("/")
def index():
    """Serve the main HTML page."""
    return render_template("index.html")

@app.route("/audio_files/<path:filename>")
def serve_audio(filename):
    """Serve generated audio files."""
    return send_from_directory(AUDIO_DIR, filename)

# ------------------------------------------------------------------#
#  TTS Endpoint                                                     #
# ------------------------------------------------------------------#
@app.route("/api/tts", methods=["POST"])
def tts_endpoint():
    """Generate TTS from text."""
    if not eleven_client:
        return jsonify({"error": "TTS service not available"}), 503

    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400
    try:
        res = generate_tts(text, eleven_client, VOICE_ID, MODEL_ID, AUDIO_DIR)
        audio_url = f"/audio_files/{os.path.basename(res['mp3_filename'])}" # Use basename for safety
        return jsonify({"message": res["message"], "audio_url": audio_url})
    except Exception as e:
        logging.exception("TTS generation failed")
        return jsonify({"error": f"TTS generation failed: {e}"}), 500

# ------------------------------------------------------------------#
#  GPT Agent Endpoint                                               #
# ------------------------------------------------------------------#
@app.route("/api/agent", methods=["POST"])
def agent_endpoint():
    """Process query with agent and generate TTS for the answer."""
    if not eleven_client:
        # Decide if agent should work without TTS
        # return jsonify({"error": "TTS service not available, cannot process agent request"}), 503
        pass # Allow agent to process, but response won't have audio

    query = (request.get_json(silent=True) or {}).get("query", "").strip()
    if not query:
        return jsonify({"error": "No query provided"}), 400

    try:
        answer = process_query(query)
    except Exception as e:
        logging.exception("Agent query processing failed")
        return jsonify({"error": f"Agent processing failed: {e}"}), 500

    # Generate TTS only if ElevenLabs client is available
    audio_url = None
    if eleven_client:
        try:
            res = generate_tts(answer, eleven_client, VOICE_ID, MODEL_ID, AUDIO_DIR)
            audio_url = f"/audio_files/{os.path.basename(res['mp3_filename'])}"
        except Exception as e:
            logging.exception("TTS generation failed for agent response")
            # Don't fail the whole request, just return without audio
            # Fallthrough intentional
    else:
        logging.warning("ElevenLabs client not available, skipping TTS for agent response.")

    return jsonify({"message": answer, "audio_url": audio_url})

# ------------------------------------------------------------------
# PDF Endpoints – upload & ask & concepts & questions
# ------------------------------------------------------------------
@app.route("/api/pdf/upload", methods=["POST"])
def pdf_upload():
    """Upload a PDF, add to vector store, set as current."""
    if 'file' not in request.files:
        return jsonify({"error": "No file part in the request"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    if not file or not file.filename.lower().endswith(".pdf"):
         return jsonify({"error": "Invalid file type, only PDF allowed"}), 400

    try:
        # Pass the Werkzeug file object directly
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
    """Ask a question about the currently active PDF."""
    data = request.get_json(silent=True) or {}
    question = data.get("question", "").strip()
    # Use the filename managed by PDFManager if not explicitly passed,
    # but it's better if frontend sends the filename it knows it uploaded.
    filename = data.get("filename")

    if not filename:
        # Fallback to the manager's current file if frontend doesn't send it
        filename = pdf_manager.get_current_filename()
        if not filename:
            return jsonify({"error": "No PDF file specified or previously uploaded in this session."}), 400
        logging.warning("Request did not specify filename, using current: %s", filename)

    if not question:
        return jsonify({"error": "No question provided"}), 400

    try:
        answer = pdf_manager.ask(filename, question)
        # Generate TTS if available
        audio_url = None
        if eleven_client:
            try:
                res = generate_tts(answer, eleven_client, VOICE_ID, MODEL_ID, AUDIO_DIR)
                audio_url = f"/audio_files/{os.path.basename(res['mp3_filename'])}"
            except Exception as e:
                logging.exception("TTS generation failed for PDF answer")
                # Continue without audio if TTS fails
        else:
            logging.warning("ElevenLabs client not available, skipping TTS for PDF answer.")

        return jsonify({"answer": answer, "audio_url": audio_url})
    except ValueError as ve:
        logging.warning("PDF ask validation error: %s", ve)
        return jsonify({"error": str(ve)}), 400
    except Exception as exc:
        logging.exception("PDF ask error")
        return jsonify({"error": f"An unexpected error occurred while asking: {exc}"}), 500

# Simplified concepts/questions using the same structure as ask
@app.route("/api/pdf/concepts", methods=["POST"])
def pdf_concepts():
    """Get key concepts from the specified PDF."""
    data = request.get_json(silent=True) or {}
    filename = data.get("filename")
    if not filename:
        filename = pdf_manager.get_current_filename()
        if not filename: return jsonify({"error":"No PDF file specified or active."}), 400
        logging.warning("Request did not specify filename, using current: %s", filename)

    try:
        concepts = pdf_manager.concepts(filename)
        return jsonify({"concepts": concepts})
    except ValueError as ve: return jsonify({"error":str(ve)}), 400
    except Exception as e:
        logging.exception("PDF concepts error")
        return jsonify({"error":f"Failed to get concepts: {e}"}), 500

@app.route("/api/pdf/questions", methods=["POST"])
def pdf_questions():
    """Generate test questions from the specified PDF."""
    data = request.get_json(silent=True) or {}
    filename = data.get("filename")
    if not filename:
        filename = pdf_manager.get_current_filename()
        if not filename: return jsonify({"error":"No PDF file specified or active."}), 400
        logging.warning("Request did not specify filename, using current: %s", filename)

    try:
        questions = pdf_manager.questions(filename)
        return jsonify({"questions": questions})
    except ValueError as ve: return jsonify({"error":str(ve)}), 400
    except Exception as e:
        logging.exception("PDF questions error")
        return jsonify({"error":f"Failed to generate questions: {e}"}), 500

# ------------------------------------------------------------------#
#  Bluetooth Endpoints                                              #
# ------------------------------------------------------------------#
@app.route("/api/bluetooth/status", methods=["GET"])
def bluetooth_status():
    """Get current Bluetooth status."""
    status = bt.get_bluetooth_status()
    return jsonify(status)

@app.route("/api/bluetooth/discoverable", methods=["POST"])
def bluetooth_discoverable():
    """Set Bluetooth discoverable state."""
    data = request.get_json(silent=True) or {}
    enable = data.get("enable", False) # Default to disabling
    duration = data.get("duration", 180) # Default duration
    try:
        success, message = bt.set_discoverable(enable, duration)
        if success:
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"success": False, "message": message}), 500
    except Exception as e:
        logging.exception("Error setting discoverable state")
        return jsonify({"success": False, "message": f"Server error: {e}"}), 500

@app.route("/api/bluetooth/pairable", methods=["POST"])
def bluetooth_pairable():
    """Set Bluetooth pairable state."""
    data = request.get_json(silent=True) or {}
    enable = data.get("enable", False) # Default to disabling
    try:
        success, message = bt.set_pairable(enable)
        if success:
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"success": False, "message": message}), 500
    except Exception as e:
        logging.exception("Error setting pairable state")
        return jsonify({"success": False, "message": f"Server error: {e}"}), 500


# ------------------------------------------------------------------#
#  SocketIO – Realtime                                              #
# ------------------------------------------------------------------#
@socketio.on("connect", namespace="/realtime")
def rt_connect():
    """Handle client connection to realtime namespace."""
    logging.info("Client connected to /realtime namespace")
    if realtime_client:
        emit("status", {"message": "Ready"})
    else:
        emit("status", {"message": "Realtime Error: Service unavailable"})


@socketio.on("disconnect", namespace="/realtime")
def rt_disconnect():
    """Handle client disconnection."""
    logging.info("Client disconnected from /realtime namespace")
    # Optional: Add any cleanup if needed when a client disconnects

@socketio.on("start_talking", namespace="/realtime")
def rt_start():
    """Start audio streaming to Realtime API."""
    if realtime_client:
        try:
            realtime_client.start_talking()
            emit("status", {"message": "Recording…"})
        except Exception as e:
            logging.exception("Error starting realtime talking")
            emit("status", {"message": f"Error: {e}"})
    else:
        emit("status", {"message": "Realtime Error: Service unavailable"})


@socketio.on("stop_talking", namespace="/realtime")
def rt_stop():
    """Stop audio streaming."""
    if realtime_client:
        try:
            realtime_client.stop_talking()
            emit("status", {"message": "Processing…"}) # Status indicates processing starts
        except Exception as e:
            logging.exception("Error stopping realtime talking")
            emit("status", {"message": f"Error: {e}"})
    else:
        emit("status", {"message": "Realtime Error: Service unavailable"})


@socketio.on("send_text", namespace="/realtime")
def rt_send_text(data):
    """Send text input to the Realtime API."""
    text = (data or {}).get("text", "").strip()
    if text and realtime_client:
        try:
            logging.info("Sending text to realtime: %s", text)
            realtime_client.send_text(text)
            # Optionally emit a status or confirmation
            # emit("status", {"message": "Text sent..."})
        except Exception as e:
            logging.exception("Error sending text to realtime")
            emit("status", {"message": f"Error sending text: {e}"})
    elif not realtime_client:
         emit("status", {"message": "Realtime Error: Service unavailable"})


# ------------------------------------------------------------------#
#  Main Execution                                                   #
# ------------------------------------------------------------------#
if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "True").lower() == "true" # Control debug via env var

    logging.info(f"Starting Flask app on {host}:{port} (Debug: {debug})")
    # Use SocketIO's run method which integrates Werkzeug server
    socketio.run(app, host=host, port=port, debug=debug, use_reloader=debug)