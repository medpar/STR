#!/usr/bin/env python3
"""
Save, convert and play audio.
Uses PyAudio for playback. Manually resamples WAV files to OUTPUT_SAMPLE_RATE,
Stereo, 16-bit using numpy before playback.
Attempts playback *only* on the configured DAC_PYAUDIO_INDEX.
"""

import os
import logging
import wave # Use standard wave module for reading
import numpy as np # Use numpy for resampling
import pyaudio

from config import DAC_PYAUDIO_INDEX, PLAYBACK_CHUNK, OUTPUT_SAMPLE_RATE

# We still need pydub for the MP3 conversion part
from pydub import AudioSegment, exceptions as pydub_exceptions


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
    # --- This function remains the same ---
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
    # --- This function remains the same ---
    try:
        log.info(f"Converting {mp3_filepath} to WAV format at {wav_filepath}...")
        audio = AudioSegment.from_mp3(mp3_filepath)
        audio.export(wav_filepath, format="wav")
        log.info(f"Successfully converted {mp3_filepath} to {wav_filepath} (Rate: {audio.frame_rate} Hz)")
    except pydub_exceptions.CouldntDecodeError as e:
        log.error(f"Pydub decoding error converting {mp3_filepath}: {e}")
        log.error("-> Ensure ffmpeg is installed and accessible in your PATH.")
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
    Manually resamples to OUTPUT_SAMPLE_RATE, Stereo, 16-bit using numpy.
    Attempts playback *only* on configured DAC_PYAUDIO_INDEX.
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
            return
        except Exception as e:
             log.error(f"Error loading WAV file {filepath} with wave module: {e}")
             return

        # --- Get Native Audio Parameters ---
        original_rate = wf.getframerate()
        original_channels = wf.getnchannels()
        original_width = wf.getsampwidth() # Bytes per sample
        num_frames = wf.getnframes()
        log.info(f" -> Loaded original: {original_rate} Hz, {original_channels} Ch, {original_width * 8}-bit, {num_frames} frames")

        # --- Read all audio data ---
        raw_data = wf.readframes(num_frames)
        wf.close() # Close the file handle now
        wf = None

        # --- Convert raw bytes to numpy array based on sample width ---
        if original_width == 1:
            dtype = np.uint8 # 8-bit is usually unsigned
        elif original_width == 2:
            dtype = np.int16 # 16-bit is usually signed
        elif original_width == 3:
             log.warning("24-bit WAV detected. Reading as bytes, conversion might be imprecise.")
             # Read as bytes, manual conversion needed later if processing
             # For now, let's hope resampling handles it okay, or error out.
             # A better approach would use soundfile or similar library.
             # We'll attempt resampling but it might fail/be wrong.
             dtype = np.uint8 # Placeholder, this isn't ideal
             # TODO: Implement proper 24-bit handling if needed
        elif original_width == 4:
            dtype = np.int32 # 32-bit is usually signed int or float
        else:
            log.error(f"Unsupported sample width: {original_width} bytes ({original_width*8}-bit)")
            return

        if dtype != np.uint8: # Don't process 24-bit further yet
             log.debug(f"Converting raw data to numpy array (dtype={dtype})...")
             samples_in = np.frombuffer(raw_data, dtype=dtype)
        elif original_width == 3: # Special handling attempt for 24-bit
             log.warning("Attempting basic 24-bit to 16-bit conversion (might lose precision/range).")
             # This is a crude approximation - assumes little-endian 24-bit signed
             samples_in = np.array([int.from_bytes(raw_data[i:i+3], 'little', signed=True) >> 8
                                   for i in range(0, len(raw_data), 3)], dtype=np.int16)
             if not samples_in.any(): # Check if conversion resulted in empty array
                 log.error("Failed to convert 24-bit audio data.")
                 return
        else: # Handle uint8 case
             samples_in = np.frombuffer(raw_data, dtype=dtype)
             # Convert uint8 (0-255) to int16 (-32768 to 32767)
             samples_in = (samples_in.astype(np.int16) - 128) * 256


        # --- Resample using numpy interpolation ---
        target_rate = OUTPUT_SAMPLE_RATE
        resampled_samples = samples_in # Default if no resampling needed
        if original_rate != target_rate:
            log.debug(f"Resampling audio from {original_rate} Hz to {target_rate} Hz using np.interp...")
            num_samples_in = len(samples_in)
            # Handle multi-channel data if original was stereo/etc.
            # We need to resample each channel independently if interleaved.
            # For simplicity now, assume resampling works ok on interleaved data,
            # or handle only the mono/stereo case correctly.
            # Let's reshape, resample first channel, then handle stereo conversion later.

            if original_channels > 1:
                # Separate channels, resample first, then decide how to combine/stereoize
                 mono_samples_in = samples_in[::original_channels] # Take first channel
                 num_mono_samples_in = len(mono_samples_in)
                 num_samples_out = int(num_mono_samples_in * target_rate / original_rate)
                 if num_mono_samples_in > 0 and num_samples_out > 0:
                     idx_orig = np.arange(num_mono_samples_in)
                     idx_new = np.linspace(0, num_mono_samples_in - 1, num_samples_out)
                     resampled_mono = np.interp(idx_new, idx_orig, mono_samples_in)
                     log.debug(f" -> Resampled MONO audio from {num_mono_samples_in} to {len(resampled_mono)} samples.")
                     # We will force stereo later using this resampled mono data
                     resampled_samples = resampled_mono # Keep the mono result for now
                 else:
                     log.warning("Cannot resample zero-length mono audio component.")
                     return
            else: # Original was mono
                 num_samples_out = int(num_samples_in * target_rate / original_rate)
                 if num_samples_in > 0 and num_samples_out > 0:
                     idx_orig = np.arange(num_samples_in)
                     idx_new = np.linspace(0, num_samples_in - 1, num_samples_out)
                     resampled_samples = np.interp(idx_new, idx_orig, samples_in)
                     log.debug(f" -> Resampled MONO audio from {num_samples_in} to {len(resampled_samples)} samples.")
                 else:
                    log.warning("Cannot resample zero-length audio.")
                    return
        else:
            log.debug("Audio rate already matches target playback rate. No resampling needed.")
            # If no resampling needed but original was multichannel, extract first channel for stereo conversion
            if original_channels > 1:
                 resampled_samples = samples_in[::original_channels]

        # --- Convert to Stereo, 16-bit ---
        target_channels = 2
        target_width = 2
        target_format_pyaudio = p.get_format_from_width(target_width) # paInt16

        log.debug("Converting resampled audio to Stereo 16-bit...")
        # At this point, resampled_samples should contain mono data at target_rate
        final_samples = np.repeat(resampled_samples, target_channels).astype(np.int16)
        output_data = final_samples.tobytes()
        log.info(f" -> Final format for playback: {target_rate} Hz, {target_channels} Ch, {target_width * 8}-bit")

        # --- Check if DAC supports the target format ---
        target_device_index = DAC_PYAUDIO_INDEX
        try:
            device_info = p.get_device_info_by_index(target_device_index)
            log.info(f"Checking format support for device: Index={target_device_index}, Name='{device_info.get('name', 'N/A')}'")
            is_supported = p.is_format_supported(
                rate=target_rate,
                input_device=None,
                input_channels=0,
                input_format=None, # Not checking input
                output_device=target_device_index,
                output_channels=target_channels,
                output_format=target_format_pyaudio
            )
            if is_supported:
                log.info(f" -> Device reports support for {target_rate} Hz, {target_channels} Ch, {target_width*8}-bit.")
            else:
                # Log a strong warning but proceed anyway - sometimes is_format_supported is unreliable
                log.warning(f" -> WARNING: Device *reports no support* for {target_rate} Hz, {target_channels} Ch, {target_width*8}-bit. Playback might fail or be incorrect!")
                # You could choose to return here if the check fails:
                # log.error("Aborting playback as target format reportedly not supported.")
                # return

        except ValueError as e:
             log.error(f"Error checking format support for device index {target_device_index}: {e}")
             # This might happen if the index is invalid - stop here.
             return
        except Exception as e:
             log.exception(f"Unexpected error checking format support for device index {target_device_index}: {e}")
             # Continue with caution? Or stop? Let's stop.
             return


        # --- Attempt to Open Stream ONLY on configured DAC ---
        stream = None
        opened_device_info = f"Configured DAC Index={target_device_index}"
        try:
            log.info(f"Attempting to open {opened_device_info} with {target_rate} Hz, {target_channels} Ch, {target_width*8}-bit...")
            stream = p.open(
                format=target_format_pyaudio,
                channels=target_channels,
                rate=target_rate, # Use the TARGET rate
                output=True,
                output_device_index=target_device_index, # Force this index
                frames_per_buffer=PLAYBACK_CHUNK,
            )
            log.info(f"Successfully opened {opened_device_info}")

        except Exception as e_dac:
            # NO FALLBACK - Log the error clearly and exit
            log.error(f"FATAL: Failed to open configured DAC (Index={target_device_index}) with the target format.")
            log.error(f" -> PyAudio Error: {e_dac}")
            log.error(" -> Check if the DAC is correctly configured in ALSA and detected by PyAudio.")
            log.error(f" -> Ensure the DAC actually supports {target_rate} Hz / {target_channels} Ch / 16-bit.")
            return # Cannot play

        # --- Play Audio ---
        log.info(f"Playing '{os.path.basename(filepath)}' ({len(output_data)} bytes)...")
        data_idx = 0
        chunk_size_bytes = PLAYBACK_CHUNK * target_channels * target_width
        while data_idx < len(output_data):
             chunk = output_data[data_idx : data_idx + chunk_size_bytes]
             stream.write(chunk)
             data_idx += len(chunk)

        stream.stop_stream()
        log.info(f"Finished playing: {os.path.basename(filepath)}")

    except FileNotFoundError:
         log.error(f"Playback Error: File not found - '{filepath}'") # Should be caught earlier
    except Exception as e:
        log.exception(f"Playback Error: An unexpected error occurred while processing/playing '{filepath}': {e}")

    finally:
        # --- Cleanup ---
        if stream is not None:
            try:
                if stream.is_active(): stream.stop_stream()
                stream.close()
                log.debug("PyAudio stream closed.")
            except Exception as e_close:
                log.error(f"Error closing PyAudio stream: {e_close}")
        if wf is not None: # Should be closed already, but just in case
            try: wf.close()
            except Exception: pass