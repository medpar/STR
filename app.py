# app.py

#!/usr/bin/env python3
"""
Flask / Flask‑SocketIO server for STR, now with PDF concepts & questions,
chat UI, and Bluetooth control removed.
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
from debug import bp as debug_bp   # <— new


# --------------------------------------------------
# Basic Setup
# --------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)5s | %(name)s | %(message)s",
)
load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "audio_files")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# --------------------------------------------------
# API Keys & Config
# --------------------------------------------------
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
VOICE_ID = os.getenv("VOICE_ID")
MODEL_ID = os.getenv("MODEL_ID")
if not all([ELEVENLABS_API_KEY, VOICE_ID, MODEL_ID]):
    logging.warning("Missing ElevenLabs API Key, Voice ID, or Model ID. TTS features might be limited.")

# --------------------------------------------------
# Flask App and Extensions Initialization
# --------------------------------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'default-secret-key-please-change')
socketio = SocketIO(app, cors_allowed_origins="*")

app.register_blueprint(debug_bp)   # <— new


# --------------------------------------------------
# Service Clients Initialization
# --------------------------------------------------
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

# --------------------------------------------------
# Realtime Client Setup
# --------------------------------------------------
def _broadcast(msg: str):
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

# --------------------------------------------------
# Basic Routes
# --------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/audio_files/<path:filename>")
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename)

# --------------------------------------------------
# TTS Endpoint
# --------------------------------------------------
@app.route("/api/tts", methods=["POST"])
def tts_endpoint():
    if not eleven_client:
        return jsonify({"error": "TTS service not available"}), 503

    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400
    try:
        res = generate_tts(text, eleven_client, VOICE_ID, MODEL_ID, AUDIO_DIR)
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
        except Exception:
            logging.exception("TTS generation failed for agent response")

    return jsonify({"message": answer, "audio_url": audio_url})

# --------------------------------------------------
# PDF Endpoints – upload & ask & concepts & questions
# --------------------------------------------------
@app.route("/api/pdf/upload", methods=["POST"])
def pdf_upload():
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
        if eleven_client:
            try:
                res = generate_tts(answer, eleven_client, VOICE_ID, MODEL_ID, AUDIO_DIR)
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
    logging.info("Client connected to /realtime namespace (sid: %s)", request.sid)
    emit("status", {"message": "Ready"} if realtime_client else {"message": "Realtime Error: Service unavailable"})

@socketio.on("disconnect", namespace="/realtime")
def rt_disconnect():
    logging.info("Client disconnected from /realtime namespace (sid: %s)", request.sid)

@socketio.on("start_talking", namespace="/realtime")
def rt_start():
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

# --------------------------------------------------
# Main Execution
# --------------------------------------------------
if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_PORT", "5000"))
    debug_env = os.getenv("FLASK_DEBUG", "False").lower()
    run_in_debug_mode = debug_env in ["true", "1", "yes"]

    logging.info(f"Starting Flask app on {host}:{port} (Debug: {run_in_debug_mode})")
    socketio.run(app, host=host, port=port, debug=run_in_debug_mode, use_reloader=run_in_debug_mode)
