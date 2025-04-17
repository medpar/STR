# ================================================
# File: /app.py
# ================================================
#!/usr/bin/env python3
"""
Flask / Flask‑SocketIO server for STR, now with PDF concepts & questions,
chat UI, and Bluetooth control using combined commands.
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
from config import MIC_DEVICE_INDEX, VECTOR_STORE_ID
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
    logging.warning("Missing ElevenLabs API Key, Voice ID, or Model ID. TTS features might be limited.")

# ------------------------------------------------------------------
# Flask App and Extensions Initialization
# ------------------------------------------------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'default-secret-key-please-change')
socketio = SocketIO(app, cors_allowed_origins="*")

# ------------------------------------------------------------------
# Service Clients Initialization
# ------------------------------------------------------------------
try:
    eleven_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)
except Exception as e:
    logging.error("Failed to initialize ElevenLabs client: %s", e)
    eleven_client = None

try:
    openai_client = OpenAI()
except Exception as e:
    logging.error("Failed to initialize OpenAI client: %s", e)
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
        voice="ash",
        mic_index=MIC_DEVICE_INDEX,
        on_text=_broadcast,
    )
except Exception as e:
    logging.error("Failed to initialize RealtimeClient: %s", e)
    realtime_client = None


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
    # Use safe_join to prevent path traversal issues, although send_from_directory handles it
    # safe_path = safe_join(AUDIO_DIR, filename) # Requires import safe_join from werkzeug.utils
    # Be cautious if filenames could contain '..'
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
        # Ensure filename does not contain path characters
        safe_filename = os.path.basename(res.get('mp3_filename', ''))
        if not safe_filename:
             raise ValueError("Invalid filename received from TTS generation")
        audio_url = f"/audio_files/{safe_filename}"
        return jsonify({"message": res.get("message", text), "audio_url": audio_url})
    except Exception as e:
        logging.exception("TTS generation failed")
        return jsonify({"error": f"TTS generation failed: {e}"}), 500

# ------------------------------------------------------------------#
#  GPT Agent Endpoint                                               #
# ------------------------------------------------------------------#
@app.route("/api/agent", methods=["POST"])
def agent_endpoint():
    """Process query with agent and generate TTS for the answer."""
    query = (request.get_json(silent=True) or {}).get("query", "").strip()
    if not query:
        return jsonify({"error": "No query provided"}), 400

    try:
        answer = process_query(query)
    except Exception as e:
        logging.exception("Agent query processing failed")
        return jsonify({"error": f"Agent processing failed: {e}"}), 500

    audio_url = None
    if eleven_client:
        try:
            res = generate_tts(answer, eleven_client, VOICE_ID, MODEL_ID, AUDIO_DIR)
            safe_filename = os.path.basename(res.get('mp3_filename', ''))
            if safe_filename:
                 audio_url = f"/audio_files/{safe_filename}"
        except Exception as e:
            logging.exception("TTS generation failed for agent response")
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
    # Secure filename before further processing
    # filename = secure_filename(file.filename) # Requires import secure_filename
    # if not filename.lower().endswith(".pdf"):
    if not file.filename.lower().endswith(".pdf"): # Basic check
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
    filename = data.get("filename") # Frontend should send filename from upload response

    if not filename:
        filename = pdf_manager.get_current_filename()
        if not filename:
            return jsonify({"error": "No PDF file specified or previously uploaded in this session."}), 400
        logging.warning("Request did not specify filename, using current: %s", filename)

    if not question:
        return jsonify({"error": "No question provided"}), 400

    try:
        answer = pdf_manager.ask(filename, question)
        audio_url = None
        if eleven_client:
            try:
                res = generate_tts(answer, eleven_client, VOICE_ID, MODEL_ID, AUDIO_DIR)
                safe_filename = os.path.basename(res.get('mp3_filename', ''))
                if safe_filename:
                    audio_url = f"/audio_files/{safe_filename}"
            except Exception as e:
                logging.exception("TTS generation failed for PDF answer")
        else:
            logging.warning("ElevenLabs client not available, skipping TTS for PDF answer.")

        return jsonify({"answer": answer, "audio_url": audio_url})
    except ValueError as ve:
        logging.warning("PDF ask validation error: %s", ve)
        return jsonify({"error": str(ve)}), 400
    except Exception as exc:
        logging.exception("PDF ask error")
        return jsonify({"error": f"An unexpected error occurred while asking: {exc}"}), 500

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
    # Return 500 if there was an error fetching status
    if status.get("error"):
         return jsonify(status), 500
    return jsonify(status)

# NEW Endpoint to set both discoverable and pairable states
@app.route("/api/bluetooth/set_mode", methods=["POST"])
def bluetooth_set_mode():
    """Set Bluetooth discoverable AND pairable state together."""
    data = request.get_json(silent=True) or {}
    enable = data.get("enable", False) # Default to disabling
    duration = data.get("duration", 180) # Default duration if enabling

    # Validate enable is a boolean
    if not isinstance(enable, bool):
         return jsonify({"success": False, "message": "Invalid 'enable' parameter, must be true or false."}), 400

    try:
        success, message = bt.set_discoverable_pairable(enable, duration)
        if success:
            return jsonify({"success": True, "message": message})
        else:
            # Use 500 for server-side/command errors, 400 for bad input (handled above)
            return jsonify({"success": False, "message": message}), 500
    except Exception as e:
        logging.exception("Error setting Bluetooth discoverable/pairable state")
        return jsonify({"success": False, "message": f"Server error: {e}"}), 500

# Remove or comment out old separate endpoints if no longer used
# @app.route("/api/bluetooth/discoverable", methods=["POST"])
# def bluetooth_discoverable(): ...

# @app.route("/api/bluetooth/pairable", methods=["POST"])
# def bluetooth_pairable(): ...


# ------------------------------------------------------------------#
#  SocketIO – Realtime                                              #
# ------------------------------------------------------------------#
@socketio.on("connect", namespace="/realtime")
def rt_connect():
    """Handle client connection to realtime namespace."""
    logging.info("Client connected to /realtime namespace (sid: %s)", request.sid)
    if realtime_client:
        emit("status", {"message": "Ready"})
    else:
        emit("status", {"message": "Realtime Error: Service unavailable"})


@socketio.on("disconnect", namespace="/realtime")
def rt_disconnect():
    """Handle client disconnection."""
    logging.info("Client disconnected from /realtime namespace (sid: %s)", request.sid)


@socketio.on("start_talking", namespace="/realtime")
def rt_start():
    """Start audio streaming to Realtime API."""
    if realtime_client:
        try:
            logging.info("rt_start: Requesting start talking (sid: %s)", request.sid)
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
            logging.info("rt_stop: Requesting stop talking (sid: %s)", request.sid)
            realtime_client.stop_talking()
            emit("status", {"message": "Processing…"})
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
            logging.info("rt_send_text: Sending text to realtime: '%s' (sid: %s)", text, request.sid)
            realtime_client.send_text(text)
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
    # Default to debug=False unless explicitly set
    debug_env = os.getenv("FLASK_DEBUG", "False").lower()
    run_in_debug_mode = debug_env in ["true", "1", "yes"]

    logging.info(f"Starting Flask app on {host}:{port} (Debug: {run_in_debug_mode})")
    # Use SocketIO's run method which integrates Werkzeug server
    socketio.run(app, host=host, port=port, debug=run_in_debug_mode, use_reloader=run_in_debug_mode)