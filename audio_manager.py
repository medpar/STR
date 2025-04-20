#!/usr/bin/env python3
"""
Save, convert and play audio – now resampling and with device fallback.
Uses PyAudio for playback.
"""

import os
import logging
from pydub import AudioSegment
import pyaudio
import wave # Added for WAV reading

from config import DAC_PYAUDIO_INDEX, PLAYBACK_CHUNK, OUTPUT_SAMPLE_RATE

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
        _p.terminate()
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
    """Convert MP3 -> WAV using pydub. Does not resample here."""
    # Resampling is handled during playback to ensure consistency
    try:
        log.info(f"Converting {mp3_filepath} to WAV format at {wav_filepath}...")
        audio = AudioSegment.from_mp3(mp3_filepath)
        # Export directly without resampling here; play_audio will handle it.
        audio.export(wav_filepath, format="wav")
        log.info(f"Successfully converted {mp3_filepath} to {wav_filepath}")
    except Exception as e:
        log.error(f"Error converting {mp3_filepath} to WAV: {e}")
        raise


def play_audio(filepath):
    """
    Play a WAV audio file using PyAudio.
    Resamples to OUTPUT_SAMPLE_RATE.
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
        # --- Load WAV file ---
        wf = wave.open(filepath, 'rb')
        original_rate = wf.getframerate()
        original_channels = wf.getnchannels()
        original_width = wf.getsampwidth()
        n_frames = wf.getnframes()
        wav_data = wf.readframes(n_frames)
        wf.close() # Close file handle
        wf = None

        log.info(f"Loaded '{os.path.basename(filepath)}': {original_rate} Hz, {original_channels} Ch, {original_width * 8}-bit")

        # --- Convert to AudioSegment for Resampling/Channel/Width adjustments ---
        audio = AudioSegment(
            data=wav_data,
            sample_width=original_width,
            frame_rate=original_rate,
            channels=original_channels
        )

        # --- Resample and format for output device ---
        log.debug(f"Resampling/formatting for output: {OUTPUT_SAMPLE_RATE} Hz, Stereo, 16-bit")
        audio = audio.set_frame_rate(OUTPUT_SAMPLE_RATE) # Resample
        audio = audio.set_channels(2) # Force Stereo
        audio = audio.set_sample_width(2) # Force 16-bit

        output_data = audio.raw_data
        output_channels = audio.channels # Should be 2
        output_width = audio.sample_width # Should be 2 (16-bit)
        output_rate = audio.frame_rate # Should be OUTPUT_SAMPLE_RATE
        output_format = p.get_format_from_width(output_width)

        # --- Attempt to Open Stream (Primary DAC first, then Fallback) ---
        target_device_info = None
        try:
            target_device_info = p.get_device_info_by_index(DAC_PYAUDIO_INDEX)
            log.info(f"Attempting to play on configured DAC: Index={DAC_PYAUDIO_INDEX}, Name='{target_device_info.get('name', 'N/A')}'")
            stream = p.open(
                format=output_format,
                channels=output_channels,
                rate=output_rate,
                output=True,
                output_device_index=DAC_PYAUDIO_INDEX,
                frames_per_buffer=PLAYBACK_CHUNK,
            )
            log.info(f"Successfully opened DAC: Index={DAC_PYAUDIO_INDEX}")

        except Exception as e_dac:
            log.warning(f"Failed to open configured DAC (Index={DAC_PYAUDIO_INDEX}): {e_dac}. Attempting default output device.")
            target_device_info = None # Reset info
            try:
                default_output_info = p.get_default_output_device_info()
                default_output_index = default_output_info['index']
                log.info(f"Attempting to play on default output device: Index={default_output_index}, Name='{default_output_info.get('name', 'N/A')}'")
                stream = p.open(
                    format=output_format,
                    channels=output_channels,
                    rate=output_rate,
                    output=True,
                    # Default device is used if output_device_index is omitted or None
                    # output_device_index=default_output_index, # Explicitly specifying can sometimes cause issues, let PyAudio choose default
                    frames_per_buffer=PLAYBACK_CHUNK,
                )
                log.info(f"Successfully opened default output device: Index={default_output_index}")
            except Exception as e_default:
                log.error(f"FATAL: Failed to open both specified DAC and default output device: {e_default}")
                # Explicitly terminate here if we failed completely
                # terminate_pyaudio_instance() # Let finally block handle it
                return # Cannot play

        # --- Play Audio ---
        log.info(f"Playing '{os.path.basename(filepath)}' ({len(output_data)} bytes)...")
        # Write data in chunks to prevent blocking and potential buffer issues
        chunk_size = PLAYBACK_CHUNK * output_channels * output_width # Bytes per chunk
        for i in range(0, len(output_data), chunk_size):
             stream.write(output_data[i:i+chunk_size])

        # Wait for stream to finish (important!)
        stream.stop_stream()
        log.info(f"Finished playing: {os.path.basename(filepath)}")

    except wave.Error as e:
        log.error(f"Playback Error: Invalid WAV file '{filepath}': {e}")
    except FileNotFoundError:
         log.error(f"Playback Error: File not found during loading - '{filepath}'") # Should be caught earlier, but defensive
    except Exception as e:
        log.exception(f"Playback Error: An unexpected error occurred while playing '{filepath}': {e}") # Log full traceback

    finally:
        # --- Cleanup ---
        if stream is not None:
            try:
                if stream.is_active():
                    stream.stop_stream()
                stream.close()
                log.debug("PyAudio stream closed.")
            except Exception as e_close:
                log.error(f"Error closing PyAudio stream: {e_close}")
        if wf is not None: # Ensure wave file handle is closed if error occurred during loading
             try:
                 wf.close()
             except Exception: pass
        # Don't terminate the shared instance here, let the main app control it or terminate on exit.
        # terminate_pyaudio_instance() # Keep instance alive for subsequent plays