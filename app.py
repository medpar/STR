import os
import logging
from flask import Flask, request, render_template, jsonify, send_from_directory
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv
from elevenlabs import ElevenLabs
from agents import process_query
from tts import generate_tts
from realtime import RealtimeClient, INSTRUCTIONS

# Configure logging
logging.basicConfig(level=logging.INFO)

# Load environment variables
load_dotenv()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
VOICE_ID = os.getenv("VOICE_ID")
MODEL_ID = os.getenv("MODEL_ID")

if not ELEVENLABS_API_KEY or not VOICE_ID or not MODEL_ID:
    logging.error("Missing configuration in .env. Please set ELEVENLABS_API_KEY, VOICE_ID, and MODEL_ID.")
    exit(1)

# Set up audio directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "audio_files")
os.makedirs(AUDIO_DIR, exist_ok=True)

# Initialize Flask app and ElevenLabs client
app = Flask(__name__)
client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

# Initialize SocketIO
socketio = SocketIO(app)

# Initialize RealtimeClient
realtime_client = RealtimeClient(instructions=INSTRUCTIONS, voice="alloy")

@app.route('/audio_files/<path:filename>')
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/tts', methods=['POST'])
def tts_endpoint():
    data = request.get_json()
    if not data or 'text' not in data:
        return jsonify({'error': 'No text provided'}), 400
    text = data['text']
    logging.info(f"TTS request received: {text}")
    try:
        result = generate_tts(text, client, VOICE_ID, MODEL_ID, AUDIO_DIR)
        audio_url = f"/audio_files/{result['mp3_filename']}"
        return jsonify({
            'message': result['message'],
            'audio_url': audio_url
        })
    except Exception as e:
        logging.exception("Error processing TTS request")
        return jsonify({'error': str(e)}), 500

@app.route('/api/agent', methods=['POST'])
def agent_endpoint():
    data = request.get_json()
    if not data or 'query' not in data:
        return jsonify({'error': 'No query provided'}), 400
    query = data['query']
    logging.info(f"Agent request received: {query}")
    try:
        broadcast_text = process_query(query)
        logging.info(f"Broadcast text: {broadcast_text}")
        result = generate_tts(broadcast_text, client, VOICE_ID, MODEL_ID, AUDIO_DIR)
        audio_url = f"/audio_files/{result['mp3_filename']}"
        return jsonify({
            'message': broadcast_text,
            'audio_url': audio_url
        })
    except Exception as e:
        logging.exception("Error processing agent request")
        return jsonify({'error': str(e)}), 500

@socketio.on('connect', namespace='/realtime')
def handle_realtime_connect():
    emit('status', {'message': 'Connected to real-time namespace'})

@socketio.on('audio_chunk', namespace='/realtime')
def handle_audio_chunk(data):
    realtime_client.send_audio_chunk(data)

@socketio.on('stop_talking', namespace='/realtime')
def handle_stop_talking():
    realtime_client.commit_and_respond()

if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)