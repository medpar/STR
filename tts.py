import os
import logging
from datetime import datetime
from audio_manager import save_stream_to_file, convert_mp3_to_wav, play_audio

log = logging.getLogger(__name__) # Use logger instance

def generate_tts(text, client, voice_id, model_id, audio_dir):
    """
    Generate text-to-speech audio using the ElevenLabs API,
    convert the audio to WAV format, and play it.
    Returns a dict with message and filename information.
    """
    if not client:
        log.error("ElevenLabs client not initialized. Cannot generate TTS.")
        raise RuntimeError("TTS client not available.")

    try:
        log.info(f"Requesting TTS from ElevenLabs for text: '{text[:50]}...'")
        # Requesting mp3 at 44.1kHz, 128kbps
        stream = client.text_to_speech.convert_as_stream(
            voice_id=voice_id,
            output_format="mp3_44100_128", # Explicitly request 44.1kHz
            text=text,
            model_id=model_id,
        )

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        mp3_filename = f"tts_{timestamp}.mp3"
        wav_filename = f"tts_{timestamp}.wav" # Will be resampled on playback
        mp3_filepath = os.path.join(audio_dir, mp3_filename)
        wav_filepath = os.path.join(audio_dir, wav_filename)

        # Ensure audio directory exists
        os.makedirs(audio_dir, exist_ok=True)

        # Save the MP3 file from the streaming response
        log.debug(f"Saving TTS MP3 stream to: {mp3_filepath}")
        save_stream_to_file(stream, mp3_filepath)

        # Convert MP3 to WAV for hardware playback (no resampling here)
        log.debug(f"Converting TTS MP3 to WAV: {wav_filepath}")
        convert_mp3_to_wav(mp3_filepath, wav_filepath)

        # Play the WAV file using audio_manager (will handle resampling)
        log.info(f"Playing generated TTS WAV file: {wav_filepath}")
        play_audio(wav_filepath) # audio_manager handles resampling to OUTPUT_SAMPLE_RATE

        log.info(f"TTS generation and playback complete for: '{text[:50]}...'")
        return {
            "message": text,
            "mp3_filename": mp3_filename, # Return MP3 name for URL
            "wav_filepath": wav_filepath # Return WAV path for reference
        }
    except Exception as e:
        log.exception("Error generating or playing TTS")
        # Consider removing partial files on error?
        # if os.path.exists(mp3_filepath): os.remove(mp3_filepath)
        # if os.path.exists(wav_filepath): os.remove(wav_filepath)
        raise e