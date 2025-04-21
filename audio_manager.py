#!/usr/bin/env python3
"""
Save, convert and play audio.
Uses PyAudio for playback. Ensures consistent output sample rate (OUTPUT_SAMPLE_RATE)
by resampling WAV files before playback using pydub.
Attempts specified DAC, falls back to default.
"""

import os
import logging
from pydub import AudioSegment, exceptions as pydub_exceptions
import pyaudio
# wave module no longer needed here for playback

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
    """Convert MP3 -> WAV using pydub. Preserves original sample rate during conversion."""
    # Resampling will happen during playback if needed
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
    Play a WAV audio file using PyAudio.
    Forces resampling to OUTPUT_SAMPLE_RATE, 16-bit Stereo using pydub before playback.
    Attempts specified DAC_PYAUDIO_INDEX first, then falls back to default output.
    """
    if not os.path.exists(filepath):
        log.error(f"Playback Error: File not found - {filepath}")
        return
    if not filepath.lower().endswith(".wav"):
        log.error(f"Playback Error: Can only play WAV files - {filepath}")
        return

    stream = None
    p = _get_pyaudio_instance() # Use shared instance

    try:
        # --- Load WAV file using pydub (handles format variations) ---
        log.info(f"Loading '{os.path.basename(filepath)}' using pydub for resampling...")
        try:
            audio = AudioSegment.from_wav(filepath)
        except pydub_exceptions.CouldntDecodeError as e:
             log.error(f"Pydub could not decode WAV file: {filepath} - {e}")
             log.error("-> The WAV file might be corrupted or in an unsupported format.")
             return
        except Exception as e:
             log.error(f"Error loading WAV file {filepath} with pydub: {e}")
             return

        original_rate = audio.frame_rate
        original_channels = audio.channels
        original_width = audio.sample_width
        log.info(f" -> Loaded original: {original_rate} Hz, {original_channels} Ch, {original_width * 8}-bit")

        # --- Force Resampling and Format Conversion for Output Device ---
        target_rate = OUTPUT_SAMPLE_RATE
        target_channels = 2 # Force Stereo (common for DACs)
        target_width = 2 # Force 16-bit (PyAudio paInt16)

        resample_needed = (original_rate != target_rate)
        channels_needed = (original_channels != target_channels)
        width_needed = (original_width != target_width)

        if resample_needed or channels_needed or width_needed:
             log.info(f"Adjusting audio format for playback: Target={target_rate}Hz, {target_channels}Ch, {target_width*8}-bit")
             try:
                 # Apply conversions sequentially
                 if resample_needed:
                      log.debug(f"   Resampling from {original_rate} Hz to {target_rate} Hz...")
                      audio = audio.set_frame_rate(target_rate)
                 if channels_needed:
                      log.debug(f"   Adjusting channels from {original_channels} to {target_channels}...")
                      audio = audio.set_channels(target_channels)
                 if width_needed:
                      log.debug(f"   Adjusting sample width from {original_width*8}-bit to {target_width*8}-bit...")
                      audio = audio.set_sample_width(target_width)
                 log.info(" -> Audio format adjusted successfully.")
             except Exception as e:
                 log.error(f"Error during pydub audio adjustment: {e}")
                 return # Stop if adjustment fails
        else:
             log.info("Audio already matches target playback format. No adjustments needed.")

        # --- Get Final Parameters and Data ---
        output_data = audio.raw_data
        output_rate = audio.frame_rate # Should match target_rate
        output_channels = audio.channels # Should match target_channels
        output_width = audio.sample_width # Should match target_width
        output_format = p.get_format_from_width(output_width) # Should be paInt16

        # --- Sanity Check ---
        if output_rate != target_rate or output_channels != target_channels or output_width != target_width:
             log.error(f"CRITICAL: Post-adjustment format mismatch! Audio is {output_rate}Hz, {output_channels}Ch, {output_width*8}-bit. Expected {target_rate}/{target_channels}/{target_width*8}.")
             return

        # --- Attempt to Open Stream (Primary DAC first, then Fallback) ---
        target_device_index = DAC_PYAUDIO_INDEX
        stream = None
        opened_device_info = "Unknown"

        try:
            device_info = p.get_device_info_by_index(target_device_index)
            opened_device_info = f"Configured DAC: Index={target_device_index}, Name='{device_info.get('name', 'N/A')}'"
            log.info(f"Attempting to play on {opened_device_info} (Rate: {output_rate} Hz, Format: {output_channels}Ch/{output_width*8}-bit)")
            stream = p.open(
                format=output_format,
                channels=output_channels,
                rate=output_rate, # Use the TARGET rate after conversion
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
                log.info(f"Attempting to play on {opened_device_info} (Rate: {output_rate} Hz, Format: {output_channels}Ch/{output_width*8}-bit)")
                stream = p.open(
                    format=output_format,
                    channels=output_channels,
                    rate=output_rate, # Use the TARGET rate after conversion
                    output=True,
                    output_device_index=None, # Let PyAudio choose default
                    frames_per_buffer=PLAYBACK_CHUNK,
                )
                log.info(f"Successfully opened {opened_device_info}")
            except Exception as e_default:
                log.error(f"FATAL: Failed to open both specified DAC and default output device: {e_default}")
                return # Cannot play

        # --- Play Audio ---
        log.info(f"Playing '{os.path.basename(filepath)}' ({len(output_data)} bytes)...")
        # Write data in chunks using a loop
        data_idx = 0
        # Use the actual chunk size based on frames_per_buffer
        chunk_size_bytes = PLAYBACK_CHUNK * output_channels * output_width
        while data_idx < len(output_data):
             chunk = output_data[data_idx : data_idx + chunk_size_bytes]
             stream.write(chunk)
             data_idx += len(chunk) # Use actual length written in case it's the last partial chunk

        # Wait for stream to finish playing the buffered data
        stream.stop_stream()
        log.info(f"Finished playing: {os.path.basename(filepath)}")

    except FileNotFoundError:
         log.error(f"Playback Error: File not found during pydub loading - '{filepath}'")
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
        # No wave object to close here as we used pydub