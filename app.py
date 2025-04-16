#!/usr/bin/env python3
"""
Flask / Flask‑SocketIO server for STR.
Adds server‑side real time speech‑to‑speech.
"""

import os
import logging

from flask import (
    Flask,
    request,
    render_template,
    jsonify,
    send_from_directory,
)
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv
from elevenlabs import ElevenLabs

from agents import process_query
from tts import generate_tts
from realtime import build_realtime_client, INSTRUCTIONS

# ----------------------------------------------------------#
#  Initialisation                                            #
# ----------------------------------------------------------#
logging.basicConfig(level=logging.INFO)
load_dotenv()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
VOICE_ID = os.getenv("VOICE_ID")
MODEL_ID = os.getenv("MODEL_ID")

if not (ELEVENLABS_API_KEY and VOICE_ID and MODEL_ID):
    raise RuntimeError(
        "Set ELEVENLABS_API_KEY, VOICE_ID and MODEL_ID in .env"
    )

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "audio_files")
os.makedirs(AUDIO_DIR, exist_ok=True)

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")  # local dev ease‑of‑use
client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

# ----------------------------------------------------------#
#  Realtime client (one global instance)                    #
# ----------------------------------------------------------#
def _broadcast_text(msg: str):
    socketio.emit("broadcast_text", {"message": msg}, namespace="/realtime")

realtime_client = build_realtime_client(on_text=_broadcast_text)

# ----------------------------------------------------------#
#  Static audio (TTS) routes                                #
# ----------------------------------------------------------#
@app.route("/audio_files/<path:filename>")
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename)


# ----------------------------------------------------------#
#  Web UI                                                   #
# ----------------------------------------------------------#
@app.route("/")
def index():
    return render_template("index.html")


# ----------------------------------------------------------#
#  API – Text‑to‑Speech                                     #
# ----------------------------------------------------------#
@app.route("/api/tts", methods=["POST"])
def tts_endpoint():
    data = request.get_json()
    if not data or "text" not in data:
        return jsonify({"error": "No text provided"}), 400

    text = data["text"]
    logging.info("TTS request: %s", text)

    try:
        res = generate_tts(text, client, VOICE_ID, MODEL_ID, AUDIO_DIR)
        return jsonify(
            {
                "message": res["message"],
                "audio_url": f"/audio_files/{res['mp3_filename']}",
            }
        )
    except Exception as exc:  # noqa: BLE001
        logging.exception("TTS failure")
        return jsonify({"error": str(exc)}), 500


# ----------------------------------------------------------#
#  API – Agent                                              #
# ----------------------------------------------------------#
@app.route("/api/agent", methods=["POST"])
def agent_endpoint():
    data = request.get_json()
    if not data or "query" not in data:
        return jsonify({"error": "No query provided"}), 400

    query = data["query"]
    logging.info("Agent query: %s", query)

    try:
        broadcast_text = process_query(query)
        res = generate_tts(
            broadcast_text, client, VOICE_ID, MODEL_ID, AUDIO_DIR
        )
        return jsonify(
            {
                "message": broadcast_text,
                "audio_url": f"/audio_files/{res['mp3_filename']}",
            }
        )
    except Exception as exc:  # noqa: BLE001
        logging.exception("Agent failure")
        return jsonify({"error": str(exc)}), 500


# ----------------------------------------------------------#
#  SocketIO – Realtime namespace                            #
# ----------------------------------------------------------#
@socketio.on("connect", namespace="/realtime")
def realtime_connect():
    emit("status", {"message": "Connected to real‑time namespace"})


@socketio.on("start_talking", namespace="/realtime")
def handle_start_talking():
    realtime_client.start_talking()
    emit("status", {"message": "Recording…"})


@socketio.on("stop_talking", namespace="/realtime")
def handle_stop_talking():
    realtime_client.stop_talking()
    emit("status", {"message": "Processing…"})


# ----------------------------------------------------------#
#  Main entry                                               #
# ----------------------------------------------------------#
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
