#!/usr/bin/env python3
"""
Save, convert and play audio – now using PyAudio for playback via DAC index.
"""

import os
import sys
import logging
import wave
import pyaudio # Added
from pydub import AudioSegment

# Import config for device index and chunk size
from config import DAC_PYAUDIO_INDEX, PLAYBACK_CHUNK

log = logging.getLogger(__name__)


# ------------------------------------------------------------------#
#  Helpers                                                          #
# ------------------------------------------------------------------#
def save_stream_to_file(stream, filepath):
    """Save streaming data to a file."""
    try:
        with open(filepath, "wb") as f:
            for chunk in stream:
                f.write(chunk)
        log.info("File saved: %s", filepath)
    except Exception as e:
        log.error(f"Error saving stream to {filepath}: {e}")
        raise

def convert_mp3_to_wav(mp3_filepath, wav_filepath):
    """Convert MP3 → WAV, 44.1 kHz stereo 16‑bit."""
    try:
        audio = AudioSegment.from_mp3(mp3_filepath)
        # Ensure consistent format for playback
        audio = audio.set_frame_rate(44100).set_sample_width(2).set_channels(2)
        audio.export(wav_filepath, format="wav")
        log.info("Converted %s → %s (44.1kHz, 16-bit, Stereo)", mp3_filepath, wav_filepath)
    except Exception as e:
        log.error(f"Error converting {mp3_filepath} to {wav_filepath}: {e}")
        raise

def play_audio(filepath):
    """Play a WAV audio file using PyAudio."""
    if not os.path.exists(filepath):
        log.error(f"Playback error: File not found - {filepath}")
        return

    wf = None
    stream = None
    p = None
    try:
        wf = wave.open(filepath, 'rb')
        p = pyaudio.PyAudio()

        sample_width = wf.getsampwidth()
        channels = wf.getnchannels()
        rate = wf.getframerate()
        audio_format = p.get_format_from_width(sample_width)

        log.info(f"Playing: {filepath} (Rate: {rate}, Ch: {channels}, Width: {sample_width}, Format: {audio_format})")
        log.info(f"Using output device index: {DAC_PYAUDIO_INDEX}")

        stream = p.open(format=audio_format,
                        channels=channels,
                        rate=rate,
                        output=True,
                        output_device_index=DAC_PYAUDIO_INDEX,
                        frames_per_buffer=PLAYBACK_CHUNK)

        data = wf.readframes(PLAYBACK_CHUNK)
        while data:
            stream.write(data)
            data = wf.readframes(PLAYBACK_CHUNK)

        # Wait for stream to finish
        stream.stop_stream()

        log.info(f"Finished playing: {filepath}")

    except FileNotFoundError:
        log.error(f"Playback failed: File not found at {filepath}")
    except Exception as e:
        log.exception(f"Error during PyAudio playback of {filepath}: {e}") # Use exception for stack trace
    finally:
        # Ensure resources are always released
        if stream is not None:
            stream.close()
            log.debug("PyAudio stream closed.")
        if wf is not None:
            wf.close()
            log.debug(f"Wave file closed: {filepath}")
        if p is not None:
            p.terminate()
            log.debug("PyAudio instance terminated.")

# Example usage if run directly (for testing)
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Create a dummy test file (e.g., a short sine wave) if needed
    test_wav = "test_playback.wav"
    if not os.path.exists(test_wav):
        try:
            from pydub.generators import Sine
            log.info("Generating a test sine wave...")
            sine_wave = Sine(440).to_audio_segment(duration=2000) # 2 seconds of 440 Hz
            # Ensure it matches expected playback format if needed, though convert_mp3_to_wav does this
            sine_wave = sine_wave.set_frame_rate(44100).set_sample_width(2).set_channels(2)
            sine_wave.export(test_wav, format="wav")
            log.info(f"Test file created: {test_wav}")
        except Exception as e:
            log.error(f"Could not create test WAV file: {e}")
            sys.exit(1)

    if os.path.exists(test_wav):
        log.info(f"Attempting to play test file: {test_wav}")
        play_audio(test_wav)
        log.info("Test playback finished.")
        # Optional: clean up test file
        # os.remove(test_wav)
    else:
        log.error("Test file does not exist, cannot perform playback test.")