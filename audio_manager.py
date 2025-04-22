# ================================================
# File: /audio_manager.py
# ================================================
#!/usr/bin/env python3
"""
Save, convert and play audio.
Uses PyAudio for playback. Manually resamples WAV files to OUTPUT_SAMPLE_RATE,
Stereo, 16-bit using resampy before playback.
Attempts playback *only* on the configured DAC_PYAUDIO_INDEX.
"""

import os
import logging
import wave # Use standard wave module for reading
import numpy as np
import pyaudio
import resampy # <<< Import resampy

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
    """Save streaming data (like from ElevenLabs) to a file. """
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
    Manually resamples to OUTPUT_SAMPLE_RATE, Stereo, 16-bit using resampy.
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
        samples_in = None # Initialize variable
        dtype = None      # Initialize variable

        if original_width == 1:
            dtype = np.uint8
            samples_in = np.frombuffer(raw_data, dtype=dtype)
            # Convert uint8 (0-255) to int16 (-32768 to 32767) range for consistent processing
            # Note: This scales the range but keeps it mono for now. Stereo conversion happens later.
            samples_in = (samples_in.astype(np.float32) - 128.0) * 256.0 # Use float for intermediate
            log.debug(" -> Converted 8-bit unsigned to float range.")
        elif original_width == 2:
            dtype = np.int16
            samples_in = np.frombuffer(raw_data, dtype=dtype) # <<< Assign for int16
        elif original_width == 3:
             log.warning("24-bit WAV detected. Reading as bytes, conversion might be imprecise.")
             try:
                 # Assign directly to samples_in here
                 samples_in = np.array([int.from_bytes(raw_data[i:i+3], 'little', signed=True) >> 8
                                        for i in range(0, len(raw_data), 3)], dtype=np.int16)
                 if not samples_in.any() and len(raw_data) > 0:
                     log.error("Failed to convert 24-bit audio data.")
                     return
                 elif len(raw_data) == 0:
                      samples_in = np.array([], dtype=np.int16) # Handle empty file case
                 dtype = np.int16 # Set effective dtype after conversion
                 log.info(" -> Attempted 24-bit to 16-bit conversion.")
             except Exception as e_24bit:
                  log.error(f"Error during 24-bit conversion: {e_24bit}. Cannot play.")
                  return
        elif original_width == 4:
            dtype = np.int32 # Can also be float32, assume int32 for now
            samples_in = np.frombuffer(raw_data, dtype=dtype)
            # Note: If it was float32, it might need different scaling later
            log.warning(" -> Loaded 32-bit audio. Assuming signed integer format.")
        else:
            log.error(f"Unsupported sample width: {original_width} bytes ({original_width*8}-bit)")
            return

        # Check if samples_in was successfully assigned
        if samples_in is None:
             log.error("Internal error: samples_in was not assigned correctly after bit depth handling.")
             return

        # Ensure samples_in is float for resampling input, regardless of original type
        samples_float = samples_in.astype(np.float32)


        # --- Prepare for Resampling ---
        target_rate = OUTPUT_SAMPLE_RATE
        samples_to_resample = samples_float # Use the float version

        # De-interleave if necessary for resampy (it prefers samples x channels for multi-channel)
        if original_channels > 1:
            try:
                # Reshape interleaved data to (num_frames, num_channels)
                num_frames_calc = len(samples_to_resample) // original_channels
                if len(samples_to_resample) % original_channels != 0:
                    log.warning(f"Audio data length ({len(samples_to_resample)}) not perfectly divisible by channel count ({original_channels}). Truncating.")
                    samples_to_resample = samples_to_resample[:num_frames_calc * original_channels] # Ensure divisibility

                if samples_to_resample.size > 0: # Avoid reshaping empty array
                    samples_to_resample = samples_to_resample.reshape((num_frames_calc, original_channels))
                    log.debug(f" -> Reshaped input to {samples_to_resample.shape} for resampling")
                else:
                    samples_to_resample = np.array([[]], dtype=samples_to_resample.dtype) # Use empty 2D array for consistency
                    log.debug(" -> Input audio is empty, creating empty shape for resampling.")

            except ValueError as e:
                log.error(f"Could not reshape multi-channel audio: {e}. Aborting playback.")
                return

        # Ensure input array isn't empty before proceeding
        if samples_to_resample.size == 0:
            log.warning("Input audio data is empty. Nothing to play.")
            return

        # --- Resample using resampy ---
        resampled_float = samples_to_resample # Default if no resampling needed
        if original_rate != target_rate:
            log.debug(f"Resampling audio from {original_rate} Hz to {target_rate} Hz using resampy...")
            try:
                # resampy works on float data and handles mono (1D) or multi-channel (2D) with axis=0
                resampled_float = resampy.resample(
                    samples_to_resample, # Already float
                    sr_orig=original_rate,
                    sr_new=target_rate,
                    filter='kaiser_fast', # Or 'kaiser_best' for higher quality
                    axis=0 # Operate along the samples axis (axis 0 for 1D or 2D)
                )
                log.debug(f" -> Resampled audio shape: {resampled_float.shape}")
            except Exception as e:
                log.exception(f"Error during resampling with resampy: {e}")
                return # Abort if resampling fails
        else:
            log.debug("Audio rate already matches target playback rate. No resampling needed.")


        # --- Convert to Target Format (Stereo, 16-bit) ---
        target_channels = 2
        target_width = 2
        target_format_pyaudio = p.get_format_from_width(target_width) # paInt16

        log.debug("Converting resampled audio to Stereo 16-bit...")

        # Determine current channels after resampling
        current_channels_resampled = 1
        if resampled_float.ndim > 1 and resampled_float.shape[1] > 0: # Check shape after resampling
            current_channels_resampled = resampled_float.shape[1]
        elif resampled_float.ndim == 1 and resampled_float.size > 0:
             current_channels_resampled = 1
        elif resampled_float.size == 0:
            log.warning("Resampled audio is empty. Nothing to play.")
            return
        else: # Unexpected shape
             log.error(f"Unexpected resampled audio shape: {resampled_float.shape}. Cannot determine channels.")
             return


        # Convert to stereo if needed
        if current_channels_resampled == 1:
             # Duplicate mono channel to create stereo: shape (n_samples,) -> (n_samples, 2)
             final_samples_stereo_float = np.column_stack((resampled_float, resampled_float))
             log.debug(" -> Duplicated mono to stereo.")
        elif current_channels_resampled == target_channels:
             # Already stereo
             final_samples_stereo_float = resampled_float
             log.debug(" -> Already stereo.")
        else: # Too many channels? Take first two? Log error?
             log.warning(f" -> Resampled audio has {current_channels_resampled} channels. Taking first {target_channels}.")
             final_samples_stereo_float = resampled_float[:, :target_channels]


        # Convert to int16 and handle clipping (important after float operations)
        # Use np.iinfo for robust clipping limits
        min_int16, max_int16 = np.iinfo(np.int16).min, np.iinfo(np.int16).max
        # Scale float data if necessary before converting to int16
        # Assuming input float data (after resampling) is roughly in -1.0 to 1.0 range OR original integer range
        # If it came from uint8, it was scaled to approx +/- 32768 float range.
        # If it came from int16, it's already in the right float range.
        # If it came from int32, it could be much larger.
        # If it came from float32 (less likely from wave), it might be -1 to 1.
        # A safe approach is to normalize to -1 to 1, then scale to int16, but this can alter dynamics.
        # Let's assume for now the float values are *somewhat* proportional to int16 and just clip.
        # This might need refinement if 32-bit sources sound too loud/quiet.
        log.debug(" -> Clipping float data to int16 range.")
        final_samples_int16 = np.clip(final_samples_stereo_float, min_int16, max_int16).astype(np.int16)

        # Convert to bytes (interleaved)
        output_data = final_samples_int16.tobytes()
        log.info(f" -> Final format for playback: {target_rate} Hz, {target_channels} Ch, {target_width * 8}-bit")

        # --- Check if DAC supports the target format ---
        target_device_index = DAC_PYAUDIO_INDEX
        try:
            device_info = p.get_device_info_by_index(target_device_index)
            log.info(f"Checking format support for device: Index={target_device_index}, Name='{device_info.get('name', 'N/A')}'")
            is_supported = p.is_format_supported(
                rate=target_rate,
                input_device=None, # Not checking input
                input_channels=0, # Not checking input
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

        except ValueError as e:
             log.error(f"Error checking format support for device index {target_device_index}: {e}")
             return
        except Exception as e:
             log.exception(f"Unexpected error checking format support for device index {target_device_index}: {e}")
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
             if not chunk: break # Exit if no more data
             stream.write(chunk)
             data_idx += len(chunk)

        # Wait for stream to finish
        stream.stop_stream()
        log.info(f"Finished playing: {os.path.basename(filepath)}")

    except FileNotFoundError:
         log.error(f"Playback Error: File not found - '{filepath}'")
    except Exception as e:
        log.exception(f"Playback Error: An unexpected error occurred while processing/playing '{filepath}': {e}")

    finally:
        # --- Cleanup ---
        if stream is not None:
            try:
                stream.close()
                log.debug("PyAudio stream closed.")
            except Exception as e_close:
                log.error(f"Error closing PyAudio stream: {e_close}")
        if wf is not None: # Should be closed already, but just in case
            try: wf.close()
            except Exception: pass