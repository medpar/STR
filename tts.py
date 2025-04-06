import os
import logging
from datetime import datetime
from audio_manager import save_stream_to_file, convert_mp3_to_wav, play_audio

def generate_tts(text, client, voice_id, model_id, audio_dir):
    """
    Generate text-to-speech audio using the ElevenLabs API,
    convert the audio to WAV format, and play it.
    Returns a dict with message and filename information.
    """
    try:
        stream = client.text_to_speech.convert_as_stream(
            voice_id=voice_id,
            output_format="mp3_44100_128",
            text=text,
            model_id=model_id,
        )
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        mp3_filename = f"tts_{timestamp}.mp3"
        wav_filename = f"tts_{timestamp}.wav"
        mp3_filepath = os.path.join(audio_dir, mp3_filename)
        wav_filepath = os.path.join(audio_dir, wav_filename)
        
        # Save the MP3 file from the streaming response
        save_stream_to_file(stream, mp3_filepath)
        # Convert MP3 to WAV for hardware playback
        convert_mp3_to_wav(mp3_filepath, wav_filepath)
        # Play the WAV file using the appropriate audio output command
        play_audio(wav_filepath)
        
        return {
            "message": text,
            "mp3_filename": mp3_filename,
            "wav_filepath": wav_filepath
        }
    except Exception as e:
        logging.exception("Error generating TTS")
        raise e
