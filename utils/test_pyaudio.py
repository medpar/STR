# ================================================
# File: test_pyaudio.py
# ================================================
#!/usr/bin/env python3
"""
PyAudio Playback Test Script for Raspberry Pi DAC Debugging.

Allows testing playback of a WAV file with specific device index,
sample rate, and bit depth (16 or 24 bit).
"""

import pyaudio
import wave
import sys
import os
import logging
import time

# --- Configuration ---
# TODO: Set these values before running!

# 1. Path to the WAV file you want to play
WAV_FILE_PATH = "audio_files/tts_20250422_010038.wav"  # <<< CHANGE THIS to your actual WAV file path


# 2. Output Device Index (Find this using the list_audio_devices() output below)
#    Common values for external DACs might be 0, 1, or higher depending on your setup.
OUTPUT_DEVICE_INDEX = 1           # <<< CHANGE THIS to the correct index for your DAC

# 3. Target Sample Rate (Hz) to test
#    Common rates: 44100, 48000, 96000. Check your DAC's capabilities.
TARGET_RATE = 44100                  # <<< CHANGE THIS (e.g., 44100, 48000, 96000)

# 4. Target Bit Depth (Choose 16 or 24)
#    NOTE: 24-bit support depends heavily on ALSA config, PyAudio version, and DAC hardware.
TARGET_BIT_DEPTH = 16               # <<< CHANGE THIS (16 or 24)

# 5. Playback buffer size (usually okay to leave as default)
CHUNK_SIZE = 512
# --- End Configuration ---

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

def list_audio_devices():
    """Prints available audio devices and their information."""
    p = pyaudio.PyAudio()
    info = p.get_host_api_info_by_index(0)
    numdevices = info.get('deviceCount')
    logging.info("Listing Available Audio Devices:")
    logging.info("-" * 40)
    for i in range(0, numdevices):
        device_info = p.get_device_info_by_index(i)
        device_name = device_info.get('name', 'N/A')
        max_out_channels = device_info.get('maxOutputChannels', 0)
        default_rate = int(device_info.get('defaultSampleRate', 0))

        log_msg = f"  Device Index: {i}"
        log_msg += f" | Name: {device_name}"
        log_msg += f" | Max Output Channels: {max_out_channels}"
        log_msg += f" | Default Rate: {default_rate} Hz"

        # Highlight potential output devices
        if max_out_channels > 0:
            log_msg += "  <<< Potential Output Device"
            logging.info(log_msg)
        else:
            # Log input devices less prominently if desired
            logging.debug(log_msg) # Use debug level for non-output devices

    logging.info("-" * 40)
    p.terminate()

def get_pyaudio_format_from_depth(bit_depth):
    """Maps target bit depth (16 or 24) to PyAudio format constant."""
    if bit_depth == 16:
        return pyaudio.paInt16
    elif bit_depth == 24:
        # Note: paInt24 support can be platform/backend dependent
        return pyaudio.paInt24
    else:
        raise ValueError(f"Unsupported target bit depth: {bit_depth}. Only 16 or 24 are supported.")

def play_test_audio(filepath, device_index, target_rate, target_bit_depth, chunk_size):
    """Attempts to play the WAV file with the specified settings."""

    logging.info(f"Attempting playback for: '{os.path.basename(filepath)}'")
    logging.info(f"  Target Device Index: {device_index}")
    logging.info(f"  Target Sample Rate: {target_rate} Hz")
    logging.info(f"  Target Bit Depth: {target_bit_depth}-bit")

    # --- 1. Check File ---
    if not os.path.exists(filepath):
        logging.error(f"Audio file not found: {filepath}")
        return False
    if not filepath.lower().endswith(".wav"):
        logging.error(f"Not a WAV file: {filepath}")
        return False

    wf = None
    p = None
    stream = None
    success = False

    try:
        # --- 2. Open WAV File ---
        try:
            wf = wave.open(filepath, 'rb')
            native_rate = wf.getframerate()
            native_channels = wf.getnchannels()
            native_width = wf.getsampwidth() # Bytes per sample
            native_bit_depth = native_width * 8
            logging.info(f"Opened WAV file: {native_rate} Hz, {native_channels} Ch, {native_bit_depth}-bit")
        except wave.Error as e:
            logging.error(f"Could not open or read WAV file '{filepath}': {e}")
            return False
        except Exception as e:
            logging.error(f"Unexpected error opening WAV file '{filepath}': {e}")
            return False

        # --- 3. Determine Target PyAudio Format ---
        try:
            target_format = get_pyaudio_format_from_depth(target_bit_depth)
            logging.info(f"Target PyAudio Format: {target_format} (for {target_bit_depth}-bit)")
        except ValueError as e:
            logging.error(e)
            return False

        # --- 4. Initialize PyAudio ---
        p = pyaudio.PyAudio()

        # --- 5. Check Format Support (Informational) ---
        logging.info(f"Checking if device {device_index} supports the target format...")
        try:
            is_supported = p.is_format_supported(
                rate=target_rate,
                input_device=None,
                input_channels=0,
                input_format=None,
                output_device=device_index,
                output_channels=native_channels, # Use channels from the WAV file
                output_format=target_format
            )
            if is_supported:
                logging.info(f" -> Device {device_index} *reports support* for {target_rate} Hz, {native_channels} Ch, {target_bit_depth}-bit.")
            else:
                logging.warning(f" -> Device {device_index} *reports NO support* for {target_rate} Hz, {native_channels} Ch, {target_bit_depth}-bit.")
                logging.warning("    Playback might fail or sound incorrect. Proceeding with attempt anyway.")
        except ValueError as e:
             logging.error(f" -> Error checking format support for device index {device_index}: {e}")
             # Don't necessarily exit, let the open attempt handle it
        except Exception as e:
             logging.error(f" -> Unexpected error checking format support: {e}")


        # --- 6. Open PyAudio Stream ---
        logging.info(f"Attempting to open stream on device {device_index}...")
        try:
            stream = p.open(
                format=target_format,         # Use TARGET format
                channels=native_channels,     # Use channels from WAV file
                rate=target_rate,             # Use TARGET rate
                output=True,                  # Output stream
                #output_device_index=device_index, # Specify target device
                frames_per_buffer=chunk_size
            )
            logging.info(f"Successfully opened stream on device {device_index}.")
        except OSError as e:
            logging.error(f"Failed to open stream on device {device_index}: {e}")
            logging.error(" -> Check if the device index is correct and the device is available.")
            logging.error(f" -> Ensure the device supports the requested format ({target_rate} Hz, {native_channels} Ch, {target_bit_depth}-bit).")
            return False
        except Exception as e:
            logging.error(f"Unexpected error opening stream: {e}")
            return False


        # --- 7. Playback Loop ---
        logging.info("Starting playback...")
        data = wf.readframes(chunk_size)
        start_time = time.time()
        frames_played = 0

        while len(data) > 0:
            try:
                stream.write(data)
                frames_played += chunk_size
                data = wf.readframes(chunk_size)
            except IOError as e:
                # Common issue is buffer underrun/overflow, especially on slower systems
                logging.warning(f"IOError during playback: {e}. Check system load.")
                # You might want to add a small sleep here if underruns are frequent
                # time.sleep(0.01)
                # Continue trying to play
            except Exception as e:
                logging.error(f"Unexpected error during stream write: {e}")
                break # Stop playback on unexpected error

        duration = time.time() - start_time
        logging.info(f"Playback finished. Played approx {frames_played / native_rate:.2f} seconds of audio in {duration:.2f} seconds.")
        success = True

    except Exception as e:
        logging.exception(f"An unexpected error occurred during the playback process: {e}")
        success = False

    finally:
        # --- 8. Cleanup ---
        logging.info("Cleaning up resources...")
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
                logging.info("Stream closed.")
            except Exception as e:
                logging.error(f"Error closing stream: {e}")
        if wf is not None:
            try:
                wf.close()
                logging.info("WAV file closed.")
            except Exception as e:
                 logging.error(f"Error closing WAV file: {e}")
        if p is not None:
            try:
                p.terminate()
                logging.info("PyAudio terminated.")
            except Exception as e:
                logging.error(f"Error terminating PyAudio: {e}")

    return success

# ================================================
# Main Execution
# ================================================
if __name__ == "__main__":
    # 1. List devices to help user find the correct index
    list_audio_devices()
    print("\n" + "=" * 60)
    print(" READ THE CONFIGURATION SECTION AT THE TOP OF THIS SCRIPT ".center(60, "="))
    print(" You MUST set WAV_FILE_PATH and OUTPUT_DEVICE_INDEX ".center(60, "="))
    print("=" * 60 + "\n")

    # 2. Validate configuration before proceeding
    if OUTPUT_DEVICE_INDEX is None:
        logging.error("FATAL: OUTPUT_DEVICE_INDEX is not set. Please edit the script.")
        sys.exit(1)
    if not os.path.exists(WAV_FILE_PATH):
         logging.error(f"FATAL: WAV file not found at '{WAV_FILE_PATH}'. Please set WAV_FILE_PATH correctly.")
         sys.exit(1)
    if TARGET_BIT_DEPTH not in [16, 24]:
        logging.error(f"FATAL: Invalid TARGET_BIT_DEPTH ({TARGET_BIT_DEPTH}). Must be 16 or 24.")
        sys.exit(1)


    logging.info("Configuration:")
    logging.info(f"  WAV File: '{WAV_FILE_PATH}'")
    logging.info(f"  Output Device Index: {OUTPUT_DEVICE_INDEX}")
    logging.info(f"  Target Rate: {TARGET_RATE} Hz")
    logging.info(f"  Target Bit Depth: {TARGET_BIT_DEPTH}-bit")
    logging.info("-" * 40)

    # Give user a moment to read the config and device list
    print("Starting playback test in 3 seconds... (Press Ctrl+C to abort)")
    try:
        time.sleep(3)
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(0)

    # 3. Run the playback test
    if play_test_audio(WAV_FILE_PATH, OUTPUT_DEVICE_INDEX, TARGET_RATE, TARGET_BIT_DEPTH, CHUNK_SIZE):
        logging.info("\nPlayback test completed successfully (sound should have played).")
    else:
        logging.error("\nPlayback test failed or encountered errors.")

    print("\nScript finished.")
