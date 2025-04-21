#!/usr/bin/env python3
"""
Save, convert and play audio.
Uses PyAudio for playback. Manually resamples WAV files to OUTPUT_SAMPLE_RATE (44.1kHz),
Stereo, 16-bit using numpy before playback.

Playback Target:
- On RPi: Attempts playback *only* on the configured DAC_PYAUDIO_INDEX.
- On other platforms (e.g., macOS): Attempts playback *only* on the default system output.
"""

import os
import logging
import wave # Use standard wave module for reading
import numpy as np # Use numpy for resampling
import pyaudio

# Import specific config variables needed
from config import (
    DAC_PYAUDIO_INDEX, PLAYBACK_CHUNK, OUTPUT_SAMPLE_RATE, _IS_RPI
)

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

# --- save_stream_to_file and convert_mp3_to_wav remain unchanged ---
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
# --- End of unchanged functions ---


def play_audio(filepath):
    """
    Play a WAV audio file using PyAudio.
    Manually resamples to OUTPUT_SAMPLE_RATE (44.1kHz), Stereo, 16-bit using numpy.
    Plays on specific DAC index if on RPi, otherwise uses system default output.
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
        # (Using the same robust handling as before)
        if original_width == 1: dtype = np.uint8
        elif original_width == 2: dtype = np.int16
        elif original_width == 4: dtype = np.int32
        # Crude 24-bit approximation (consider soundfile library for better handling)
        elif original_width == 3: dtype = np.uint8; log.warning("Attempting crude 24-bit read.")
        else: log.error(f"Unsupported sample width: {original_width} bytes"); return

        samples_in = np.frombuffer(raw_data, dtype=dtype)

        # Post-process specific dtypes if necessary
        if original_width == 1: samples_in = (samples_in.astype(np.int16) - 128) * 256 # uint8 to int16
        elif original_width == 3: # Crude 24->16 conversion
            samples_in = np.array([int.from_bytes(raw_data[i:i+3], 'little', signed=True) >> 8
                                  for i in range(0, len(raw_data), 3)], dtype=np.int16)
            if not samples_in.any(): log.error("Failed to convert 24-bit audio data."); return

        # --- Resample using numpy interpolation ---
        target_rate = OUTPUT_SAMPLE_RATE # Now defaults to 44100
        resampled_samples = samples_in
        if original_rate != target_rate:
            log.debug(f"Resampling audio from {original_rate} Hz to {target_rate} Hz using np.interp...")
            num_samples_in = len(samples_in)
            # Handle multi-channel source data by taking first channel for resampling base
            source_mono = samples_in[::original_channels] if original_channels > 1 else samples_in
            num_mono_samples_in = len(source_mono)

            if num_mono_samples_in > 0:
                num_samples_out = int(num_mono_samples_in * target_rate / original_rate)
                if num_samples_out > 0:
                    idx_orig = np.arange(num_mono_samples_in)
                    idx_new = np.linspace(0, num_mono_samples_in - 1, num_samples_out)
                    resampled_samples = np.interp(idx_new, idx_orig, source_mono) # Result is mono float64
                    log.debug(f" -> Resampled MONO component to {len(resampled_samples)} samples.")
                else: log.warning("Resampling resulted in zero output samples."); return
            else: log.warning("Cannot resample zero-length audio."); return
        else:
            log.debug("Audio rate already matches target playback rate. No resampling needed.")
            # If no resampling needed but original was multichannel, extract first channel
            if original_channels > 1:
                 resampled_samples = samples_in[::original_channels] # Result is mono

        # --- Convert to Stereo, 16-bit ---
        target_channels = 2
        target_width = 2
        target_format_pyaudio = p.get_format_from_width(target_width) # paInt16

        log.debug("Converting resampled audio to Stereo 16-bit...")
        # Ensure input to repeat is 1D (mono)
        if resampled_samples.ndim != 1:
            log.error(f"Internal Error: Expected 1D array for stereo conversion, got {resampled_samples.ndim}D")
            return
        # Repeat mono samples for stereo and cast to int16
        final_samples = np.repeat(resampled_samples, target_channels).astype(np.int16)
        output_data = final_samples.tobytes()
        log.info(f" -> Final format for playback: {target_rate} Hz, {target_channels} Ch, {target_width * 8}-bit")

        # --- Determine Target Device Index based on Platform ---
        playback_device_index = None # Default to None (system default)
        device_description = "system default output device"
        if _IS_RPI:
            playback_device_index = DAC_PYAUDIO_INDEX # Use specific index on RPi
            device_description = f"configured RPi DAC (Index={playback_device_index})"
            log.info(f"Running on RPi. Targeting specific DAC index: {playback_device_index}")
        else:
            log.info(f"Not running on RPi. Targeting system default output device.")

        # --- Check format support (Optional but recommended) ---
        try:
            device_info = p.get_device_info_by_index(playback_device_index) if playback_device_index is not None else p.get_default_output_device_info()
            log.info(f"Checking format support for {device_description}: Name='{device_info.get('name', 'N/A')}'")
            is_supported = p.is_format_supported(
                rate=target_rate,
                input_device=None, input_channels=0, input_format=None,
                output_device=playback_device_index, # Use None for default
                output_channels=target_channels,
                output_format=target_format_pyaudio
            )
            if is_supported:
                log.info(f" -> Device reports support for {target_rate} Hz, {target_channels} Ch, 16-bit.")
            else:
                log.warning(f" -> WARNING: Device *reports no support* for {target_rate} Hz, {target_channels} Ch, 16-bit. Playback might fail or be incorrect!")
        except ValueError as e:
             log.error(f"Error checking format support for device {device_description}: {e}")
             return # Stop if device index is invalid or default doesn't exist
        except Exception as e:
             log.exception(f"Unexpected error checking format support for {device_description}: {e}")
             return # Stop on other errors during check


        # --- Attempt to Open Stream on the Target Device ---
        stream = None
        try:
            log.info(f"Attempting to open {device_description} with {target_rate} Hz, {target_channels} Ch, 16-bit...")
            stream = p.open(
                format=target_format_pyaudio,
                channels=target_channels,
                rate=target_rate,
                output=True,
                output_device_index=playback_device_index, # Explicitly None for default, specific index for RPi DAC
                frames_per_buffer=PLAYBACK_CHUNK,
            )
            log.info(f"Successfully opened {device_description}")

        except Exception as e_open:
            log.error(f"FATAL: Failed to open {device_description} with the target format.")
            log.error(f" -> PyAudio Error: {e_open}")
            if _IS_RPI: log.error(" -> Check RPi DAC index, ALSA config, and dtoverlay.")
            else: log.error(" -> Check system audio settings and ensure default device is working.")
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