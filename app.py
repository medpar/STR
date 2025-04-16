#!/usr/bin/env python3
"""
Flask / Flask‑SocketIO server for STR – now using file_manager.py,
plus clear‑history support and chat‑style layout.
"""

from __future__ import annotations

import os
import logging

from dotenv import load_dotenv
from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_from_directory,
)
from flask_socketio import SocketIO, emit
from elevenlabs import ElevenLabs
from openai import OpenAI

from agents import process_query
from tts import generate_tts
from realtime import RealtimeClient, INSTRUCTIONS
from file_manager import PDFManager

# ------------------------------------------------------------------#
#  Config                                                           #
# ------------------------------------------------------------------#
logging.basicConfig(level=logging.INFO)
load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "audio_files")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Keys
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
VOICE_ID = os.getenv("VOICE_ID")
MODEL_ID = os.getenv("MODEL_ID")
if not all([ELEVENLABS_API_KEY, VOICE_ID, MODEL_ID]):
    raise RuntimeError("Missing ELEVENLABS_API_KEY, VOICE_ID or MODEL_ID")

# Flask + SocketIO
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

eleven_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)
openai_client = OpenAI()

# PDF manager
pdf_manager = PDFManager(UPLOAD_DIR)

# Realtime (voice ash)
def _broadcast(msg: str):
    socketio.emit("broadcast_text", {"message": msg}, namespace="/realtime")


realtime_client = RealtimeClient(
    instructions=INSTRUCTIONS,
    voice="ash",
    mic_index=int(os.getenv("MIC_DEVICE_INDEX"))
    if os.getenv("MIC_DEVICE_INDEX")
    else None,
    on_text=_broadcast,
)

# ------------------------------------------------------------------#
#  Static / HTML                                                    #
# ------------------------------------------------------------------#
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/audio_files/<path:filename>")
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename)


# ------------------------------------------------------------------#
#  TTS                                                              #
# ------------------------------------------------------------------#
@app.route("/api/tts", methods=["POST"])
def tts_endpoint():
    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400
    res = generate_tts(text, eleven_client, VOICE_ID, MODEL_ID, AUDIO_DIR)
    return jsonify(
        {"message": res["message"], "audio_url": f"/audio_files/{res['mp3_filename']}"}
    )


# ------------------------------------------------------------------#
#  GPT Agent                                                        #
# ------------------------------------------------------------------#
@app.route("/api/agent", methods=["POST"])
def agent_endpoint():
    query = (request.get_json(silent=True) or {}).get("query", "").strip()
    if not query:
        return jsonify({"error": "No query provided"}), 400
    answer = process_query(query)
    res = generate_tts(answer, eleven_client, VOICE_ID, MODEL_ID, AUDIO_DIR)
    return jsonify(
        {"message": answer, "audio_url": f"/audio_files/{res['mp3_filename']}"}
    )


# ------------------------------------------------------------------#
#  PDF – upload & ask                                               #
# ------------------------------------------------------------------#
@app.route("/api/pdf/upload", methods=["POST"])
def pdf_upload():
    try:
        info = pdf_manager.upload(request.files.get("file"))
        return jsonify(info)
    except Exception as exc:  # noqa: BLE001
        logging.exception("PDF upload failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/pdf/ask", methods=["POST"])
def pdf_ask():
    data = request.get_json(silent=True) or {}
    question = data.get("question", "").strip()
    filename = data.get("filename")
    if not question or not filename:
        return jsonify({"error": "Invalid request"}), 400
    try:
        answer = pdf_manager.ask(filename, question)
    except Exception as exc:  # noqa: BLE001
        logging.exception("PDF ask error")
        return jsonify({"error": str(exc)}), 400
    res = generate_tts(answer, eleven_client, VOICE_ID, MODEL_ID, AUDIO_DIR)
    return jsonify(
        {"answer": answer, "audio_url": f"/audio_files/{res['mp3_filename']}"}
    )


# ------------------------------------------------------------------#
#  SocketIO – realtime                                              #
# ------------------------------------------------------------------#
@socketio.on("connect", namespace="/realtime")
def rt_connect():
    emit("status", {"message": "Ready"})


@socketio.on("start_talking", namespace="/realtime")
def rt_start():
    realtime_client.start_talking()
    emit("status", {"message": "Recording…"})


@socketio.on("stop_talking", namespace="/realtime")
def rt_stop():
    realtime_client.stop_talking()
    emit("status", {"message": "Processing…"})


@socketio.on("send_text", namespace="/realtime")
def rt_send_text(data):
    text = (data or {}).get("text", "").strip()
    if text:
        realtime_client.send_text(text)


# ------------------------------------------------------------------#
#  Main                                                             #
# ------------------------------------------------------------------#
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
