import os
import sys
import subprocess
import logging
from pydub import AudioSegment

def save_stream_to_file(stream, filepath):
    """Save streaming data to a file."""
    try:
        with open(filepath, "wb") as f:
            for chunk in stream:
                f.write(chunk)
        logging.info(f"File saved: {filepath}")
    except Exception as e:
        logging.exception("Error saving stream to file.")
        raise e

def convert_mp3_to_wav(mp3_filepath, wav_filepath):
    """Convert an MP3 file to WAV format with desired settings."""
    try:
        audio = AudioSegment.from_mp3(mp3_filepath)
        audio = audio.set_frame_rate(44100).set_sample_width(2).set_channels(2)
        audio.export(wav_filepath, format="wav")
        logging.info(f"Converted {mp3_filepath} to {wav_filepath}")
    except Exception as e:
        logging.exception("Error converting MP3 to WAV.")
        raise e

def play_audio(filepath):
    """Play an audio file using system-specific command."""
    try:
        if sys.platform.startswith("linux"):
            # Raspberry Pi: using aplay with PCM5102 DAC configuration
            play_cmd = ["aplay", "-D", "plughw:1,0", filepath]
        elif sys.platform == "darwin":
            # macOS: using afplay
            play_cmd = ["afplay", filepath]
        else:
            # Default fallback
            play_cmd = ["aplay", filepath]
        logging.info(f"Playing audio with command: {' '.join(play_cmd)}")
        subprocess.run(play_cmd, check=True)
    except Exception as e:
        logging.exception("Error playing audio.")
        raise e
