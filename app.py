import os
import logging
import subprocess
from datetime import datetime
from flask import Flask, request, render_template, jsonify, send_from_directory
from dotenv import load_dotenv
from elevenlabs import ElevenLabs
from pydub import AudioSegment
from agents import process_query
#from pyngrok import ngrok  # Deshabilitado si no se usa para túnel

# Configura el logging.
logging.basicConfig(level=logging.INFO)

# Carga las variables de entorno.
load_dotenv()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
VOICE_ID = os.getenv("VOICE_ID")
MODEL_ID = os.getenv("MODEL_ID")

if not ELEVENLABS_API_KEY or not VOICE_ID or not MODEL_ID:
    logging.error("Falta la configuración en .env. Debes establecer ELEVENLABS_API_KEY, VOICE_ID y MODEL_ID.")
    exit(1)

# Crea la carpeta para almacenar los archivos de audio.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "audio_files")
os.makedirs(AUDIO_DIR, exist_ok=True)

app = Flask(__name__)

# Inicializa el cliente de ElevenLabs.
client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

# Sirve los archivos de audio para que se puedan reproducir en la web.
@app.route('/audio_files/<path:filename>')
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/text-to-speech', methods=['POST'])
def text_to_speech():
    data = request.get_json()
    if not data or 'text' not in data:
        return jsonify({'error': 'No se proporcionó texto'}), 400

    text = data['text']
    logging.info(f"Texto recibido para TTS: {text}")

    try:
        # Obtiene el stream MP3 de ElevenLabs.
        stream = client.text_to_speech.convert_as_stream(
            voice_id=VOICE_ID,
            output_format="mp3_44100_128",
            text=text,
            model_id=MODEL_ID,
        )
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        mp3_filename = f"tts_{timestamp}.mp3"
        mp3_filepath = os.path.join(AUDIO_DIR, mp3_filename)
        wav_filename = f"tts_{timestamp}.wav"
        wav_filepath = os.path.join(AUDIO_DIR, wav_filename)

        # Guarda el archivo MP3.
        with open(mp3_filepath, "wb") as f:
            for chunk in stream:
                f.write(chunk)
        logging.info(f"Archivo MP3 guardado: {mp3_filepath}")

        # Convierte el MP3 a WAV para la salida por el DAC.
        audio = AudioSegment.from_mp3(mp3_filepath)
        audio = audio.set_frame_rate(44100).set_sample_width(2).set_channels(2)
        audio.export(wav_filepath, format="wav")
        logging.info(f"Archivo WAV generado: {wav_filepath}")

        # Reproduce el archivo WAV en el DAC (asegúrate de que el dispositivo ALSA esté correctamente configurado).
        play_cmd = ["aplay", "-D", "plughw:1,0", wav_filepath]
        subprocess.run(play_cmd, check=True)
        logging.info("Reproducción iniciada en el DAC PCM5102.")

        audio_url = f"/audio_files/{mp3_filename}"
        return jsonify({
            'message': text,  # Devuelve el texto que se ha hablado
            'audio_url': audio_url
        })
    except Exception as e:
        logging.exception("Error al procesar TTS")
        return jsonify({'error': str(e)}), 500

@app.route('/api/agent', methods=['POST'])
def agent_query():
    data = request.get_json()
    if not data or 'query' not in data:
        return jsonify({'error': 'No se proporcionó consulta'}), 400

    query = data['query']
    logging.info(f"Consulta recibida para agent: {query}")

    try:
        # Procesa la consulta usando el agente para generar el texto broadcast.
        broadcast_text = process_query(query)
        logging.info(f"Texto broadcast generado: {broadcast_text}")

        # Convierte el texto broadcast a voz usando ElevenLabs.
        stream = client.text_to_speech.convert_as_stream(
            voice_id=VOICE_ID,
            output_format="mp3_44100_128",
            text=broadcast_text,
            model_id=MODEL_ID,
        )
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        mp3_filename = f"agent_{timestamp}.mp3"
        mp3_filepath = os.path.join(AUDIO_DIR, mp3_filename)
        wav_filename = f"agent_{timestamp}.wav"
        wav_filepath = os.path.join(AUDIO_DIR, wav_filename)

        # Guarda el archivo MP3.
        with open(mp3_filepath, "wb") as f:
            for chunk in stream:
                f.write(chunk)
        logging.info(f"Archivo MP3 agent guardado: {mp3_filepath}")

        # Convierte el MP3 a WAV para la reproducción en el DAC.
        audio = AudioSegment.from_mp3(mp3_filepath)
        audio = audio.set_frame_rate(44100).set_sample_width(2).set_channels(2)
        audio.export(wav_filepath, format="wav")
        logging.info(f"Archivo WAV agent generado: {wav_filepath}")

        # Reproduce el archivo WAV en el DAC.
        play_cmd = ["aplay", "-D", "plughw:1,0", wav_filepath]
        subprocess.run(play_cmd, check=True)
        logging.info("Reproducción agent iniciada en el DAC PCM5102.")

        audio_url = f"/audio_files/{mp3_filename}"
        return jsonify({
            'message': broadcast_text,
            'audio_url': audio_url
        })
    except Exception as e:
        logging.exception("Error al procesar la consulta del agent")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    # Si deseas usar ngrok, descomenta y configura NGROK_AUTH_TOKEN en tu .env.
    # from pyngrok import ngrok
    # ngrok_auth_token = os.getenv("NGROK_AUTH_TOKEN")
    # if ngrok_auth_token:
    #     ngrok.set_auth_token(ngrok_auth_token)
    # ngrok.kill()
    # public_url = ngrok.connect(5000).public_url
    # print(" * ngrok tunnel available at:", public_url)
    
    app.run(host="0.0.0.0", port=5000, debug=True)
