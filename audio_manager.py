#!/usr/bin/env python3
"""
Save, convert and play audio.
Uses PyAudio for playback, playing files at their native sample rate/format.
Attempts specified DAC, falls back to default.
"""

import os
import logging
from pydub import AudioSegment, exceptions as pydub_exceptions
import pyaudio
import wave # Use standard wave module for playback info

from config import DAC_PYAUDIO_INDEX, PLAYBACK_CHUNK, OUTPUT_SAMPLE_RATE # OUTPUT_SAMPLE_RATE is now less critical here

log = logging.getLogger(__name__)

# Single PyAudio instance for this module
_p = None

def _get_pyaudio_instance():
    global _p
    if _p is None:
        log.debug("Initializing PyAudio instance for audio_manager.")
        _p = pyaudio.PyAudio()
    return _p

def terminate_pyaudio_instance():
    """Terminate the shared PyAudio instance if it exists."""
    global _p
    if _p is not None:
        log.debug("Terminating PyAudio instance for audio_manager.")
        try:
            _p.terminate()
        except Exception as e:
             log.error(f"Error terminating PyAudio instance in audio_manager: {e}")
        finally:
            _p = None

def save_stream_to_file(stream, filepath):
    """Save streaming data (like from ElevenLabs) to a file."""
    try:
        with open(filepath, "wb") as f:
            for chunk in stream:
                f.write(chunk)
        log.info(f"Stream saved successfully to: {filepath}")
    except Exception as e:
        log.error(f"Error saving stream to {filepath}: {e}")
        raise

def convert_mp3_to_wav(mp3_filepath, wav_filepath):
    """Convert MP3 -> WAV using pydub. Preserves original sample rate."""
    try:
        log.info(f"Converting {mp3_filepath} to WAV format at {wav_filepath}...")
        audio = AudioSegment.from_mp3(mp3_filepath)
        # Export directly, preserving the original sample rate from the MP3
        # Pydub defaults to 16-bit WAV which is generally compatible
        audio.export(wav_filepath, format="wav")
        log.info(f"Successfully converted {mp3_filepath} to {wav_filepath} (Rate: {audio.frame_rate} Hz)")
    except pydub_exceptions.CouldntDecodeError as e:
        log.error(f"Pydub decoding error converting {mp3_filepath}: {e}")
        log.error("-> Ensure ffmpeg is installed and accessible in your PATH.")
        log.error("-> Check if the MP3 file is valid/corrupted.")
        raise
    except FileNotFoundError as e:
         log.error(f"File not found during MP3 conversion: {e}")
         log.error("-> Ensure ffmpeg is installed and accessible in your PATH.")
         raise
    except Exception as e:
        log.error(f"Error converting {mp3_filepath} to WAV: {e}")
        log.error("-> Ensure ffmpeg is installed and accessible in your PATH.")
        raise

def play_audio(filepath):
    """
    Play a WAV audio file using PyAudio at its NATIVE format.
    Attempts specified DAC_PYAUDIO_INDEX first, then falls back to default output.
    """
    if not os.path.exists(filepath):
        log.error(f"Playback Error: File not found - {filepath}")
        return
    if not filepath.lower().endswith(".wav"):
        log.error(f"Playback Error: Can only play WAV files - {filepath}")
        return

    wf = None
    stream = None
    p = _get_pyaudio_instance() # Use shared instance

    try:
        # --- Load WAV file using standard wave module ---
        log.info(f"Loading '{os.path.basename(filepath)}' using wave module...")
        try:
            wf = wave.open(filepath, 'rb')
        except wave.Error as e:
            log.error(f"Wave module could not open WAV file: {filepath} - {e}")
            log.error("-> The WAV file might be corrupted or in an unsupported format.")
            return
        except Exception as e:
             log.error(f"Error loading WAV file {filepath} with wave module: {e}")
             return

        # --- Get Native Audio Parameters ---
        native_rate = wf.getframerate()
        native_channels = wf.getnchannels()
        native_width = wf.getsampwidth() # Bytes per sample
        native_format = p.get_format_from_width(native_width)
        log.info(f" -> Native Format: {native_rate} Hz, {native_channels} Ch, {native_width * 8}-bit (PyAudio Format: {native_format})")

        # --- Attempt to Open Stream (Primary DAC first, then Fallback) ---
        # Use the NATIVE format detected from the file
        target_device_index = DAC_PYAUDIO_INDEX
        stream = None
        opened_device_info = "Unknown" # For logging

        try:
            device_info = p.get_device_info_by_index(target_device_index)
            opened_device_info = f"Configured DAC: Index={target_device_index}, Name='{device_info.get('name', 'N/A')}'"
            log.info(f"Attempting to play on {opened_device_info} (Native Rate: {native_rate} Hz)")
            stream = p.open(
                format=native_format,
                channels=native_channels,
                rate=native_rate, # Use the NATIVE rate
                output=True,
                output_device_index=target_device_index,
                frames_per_buffer=PLAYBACK_CHUNK,
            )
            log.info(f"Successfully opened {opened_device_info}")

        except Exception as e_dac:
            log.warning(f"Failed to open {opened_device_info}: {e_dac}. Attempting default output device.")
            try:
                # Check if default output exists before trying to open
                default_output_info = p.get_default_output_device_info()
                default_output_index = default_output_info['index']
                opened_device_info = f"Default Output: Index={default_output_index}, Name='{default_output_info.get('name', 'N/A')}'"
                log.info(f"Attempting to play on {opened_device_info} (Native Rate: {native_rate} Hz)")
                stream = p.open(
                    format=native_format,
                    channels=native_channels,
                    rate=native_rate, # Use the NATIVE rate
                    output=True,
                    output_device_index=None, # Let PyAudio choose default
                    frames_per_buffer=PLAYBACK_CHUNK,
                )
                log.info(f"Successfully opened {opened_device_info}")
            except Exception as e_default:
                log.error(f"FATAL: Failed to open both specified DAC and default output device: {e_default}")
                if wf: wf.close() # Close wave file before returning
                return # Cannot play

        # --- Play Audio ---
        log.info(f"Playing '{os.path.basename(filepath)}'...")
        data = wf.readframes(PLAYBACK_CHUNK)
        while data:
            stream.write(data)
            data = wf.readframes(PLAYBACK_CHUNK)

        # Wait for stream to finish playing the buffered data
        stream.stop_stream()
        log.info(f"Finished playing: {os.path.basename(filepath)}")

    except FileNotFoundError:
         log.error(f"Playback Error: File not found during wave loading - '{filepath}'")
    except Exception as e:
        log.exception(f"Playback Error: An unexpected error occurred while processing/playing '{filepath}': {e}")

    finally:
        # --- Cleanup ---
        if stream is not None:
            try:
                # Ensure stream is stopped before closing, even if stop_stream was called earlier
                if stream.is_active():
                    stream.stop_stream()
                stream.close()
                log.debug("PyAudio stream closed.")
            except Exception as e_close:
                log.error(f"Error closing PyAudio stream: {e_close}")
        if wf is not None:
            try:
                wf.close()
                log.debug("Wave file closed.")
            except Exception as e_wf_close:
                log.error(f"Error closing wave file: {e_wf_close}")

        # Note: The shared PyAudio instance (_p) is terminated by terminate_pyaudio_instance(),
        # which should be called explicitly on application shutdown if needed.