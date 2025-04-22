# ================================================
# File: test_pyaudio_interactive_channels.py
# ================================================
#!/usr/bin/env python3
"""
PyAudio Playback Test Script for Raspberry Pi DAC Debugging.

Allows testing playback of a WAV file with user-specified (or interactively prompted)
device index, sample rate, bit depth, channels, and chunk size.
Includes diagnostics for playback speed issues.
"""

import pyaudio
import wave
import sys
import os
import logging
import time
import argparse

# --- Default Configuration (used if not provided via args/prompt) ---
DEFAULT_CHUNK_SIZE = 1024
# --- End Default Configuration ---

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

def list_audio_devices():
    """Prints available audio devices and their information."""
    p = None
    try:
        p = pyaudio.PyAudio()
        info = p.get_host_api_info_by_index(0) # Default host API
        numdevices = info.get('deviceCount', 0)
        logging.info("Listing Available Audio Devices:")
        logging.info("-" * 60)
        if numdevices == 0:
            logging.warning("No audio devices found via default Host API.")
            return

        found_output = False
        for i in range(numdevices):
            device_info = {}
            try:
                device_info = p.get_device_info_by_index(i)
                device_name = device_info.get('name', 'N/A')
                max_out_channels = device_info.get('maxOutputChannels', 0)
                default_rate = int(device_info.get('defaultSampleRate', 0))

                log_msg = f"  Device Index: {i}"
                log_msg += f" | Name: {device_name}"
                log_msg += f" | Max Output Channels: {max_out_channels}" # Log max output channels
                log_msg += f" | Default Rate: {default_rate} Hz"

                if max_out_channels > 0:
                    log_msg += "  <<< Potential Output Device"
                    logging.info(log_msg)
                    found_output = True
                else:
                    logging.debug(log_msg) # Use debug level for non-output devices

            except Exception as e:
                logging.error(f"  Error getting info for device index {i}: {e}")
                logging.error(f"  Device Info received: {device_info}")


        if not found_output:
             logging.warning("Could not find any potential output devices.")

        logging.info("-" * 60)
    except Exception as e:
        logging.error(f"Error initializing PyAudio or listing devices: {e}")
    finally:
        if p:
            p.terminate()


def get_pyaudio_format_from_depth(bit_depth):
    """Maps target bit depth (16 or 24) to PyAudio format constant."""
    if bit_depth == 16:
        return pyaudio.paInt16
    elif bit_depth == 24:
        return pyaudio.paInt24
    else:
        raise ValueError(f"Unsupported target bit depth: {bit_depth}. Only 16 or 24 are supported.")

# Modified function signature to include target_channels
def play_test_audio(filepath, device_index, target_rate, target_bit_depth, target_channels, chunk_size):
    """Attempts to play the WAV file with the specified settings and logs timing info."""

    logging.info(f"--- Starting Playback Test ---")
    logging.info(f"  File: '{os.path.basename(filepath)}'")
    logging.info(f"  Target Device Index: {device_index}")
    logging.info(f"  Target Sample Rate: {target_rate} Hz")
    logging.info(f"  Target Bit Depth: {target_bit_depth}-bit")
    logging.info(f"  Target Channels: {target_channels}") # Log target channels
    logging.info(f"  Chunk Size: {chunk_size} frames")
    logging.info("-" * 40)

    wf = None
    p = None
    stream = None
    success = False
    native_rate = 0
    native_channels = 0
    native_width = 0
    total_frames_read = 0

    try:
        # --- 2. Open WAV File ---
        try:
            wf = wave.open(filepath, 'rb')
            native_rate = wf.getframerate()
            native_channels = wf.getnchannels()
            native_width = wf.getsampwidth() # Bytes per sample
            native_bit_depth = native_width * 8
            total_frames_in_file = wf.getnframes()
            expected_duration = total_frames_in_file / float(native_rate) if native_rate > 0 else 0

            logging.info("Opened WAV file Properties:")
            logging.info(f"  Native Sample Rate: {native_rate} Hz")
            logging.info(f"  Native Channels: {native_channels}") # Log native channels
            logging.info(f"  Native Bit Depth: {native_bit_depth}-bit")
            logging.info(f"  Total Frames: {total_frames_in_file}")
            logging.info(f"  Expected Duration: {expected_duration:.2f} seconds")
            logging.info("-" * 40)

        except wave.Error as e:
            logging.error(f"Could not open or read WAV file '{filepath}': {e}")
            return False
        except Exception as e:
            logging.error(f"Unexpected error opening WAV file '{filepath}': {e}")
            return False

        # --- DEBUGGING: Sample Rate Mismatch ---
        if native_rate != target_rate:
            logging.warning("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            logging.warning(f"SAMPLE RATE MISMATCH DETECTED!")
            logging.warning(f"  WAV file rate is {native_rate} Hz.")
            logging.warning(f"  You requested playback at {target_rate} Hz.")
            logging.warning("  --> This is the MOST LIKELY cause of slow/fast playback or incorrect pitch.")
            logging.warning("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        else:
             logging.info("Target sample rate matches the WAV file's native sample rate.")

        # --- DEBUGGING: Channel Mismatch ---
        if native_channels != target_channels:
            logging.warning("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            logging.warning(f"CHANNEL COUNT MISMATCH DETECTED!")
            logging.warning(f"  WAV file has {native_channels} channel(s).")
            logging.warning(f"  You requested playback using {target_channels} channel(s).")
            logging.warning("  --> This might cause unexpected audio output (e.g., silent channels, mixed audio) or errors.")
            logging.warning("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        else:
            logging.info("Target channel count matches the WAV file's native channel count.")


        # --- 3. Determine Target PyAudio Format ---
        try:
            target_format = get_pyaudio_format_from_depth(target_bit_depth)
            logging.info(f"Requesting PyAudio Format: {target_format} (for {target_bit_depth}-bit)")
        except ValueError as e:
            logging.error(e)
            return False

        # --- 4. Initialize PyAudio ---
        p = pyaudio.PyAudio()

        # --- 5. Open PyAudio Stream ---
        logging.info(f"Attempting to open stream on device {device_index} with TARGET settings...")
        stream_start_time = time.time()
        try:
            stream = p.open(
                format=target_format,
                channels=target_channels,     # Use TARGET channels
                rate=target_rate,
                output=True,
                output_device_index=device_index,
                frames_per_buffer=chunk_size
            )
            stream_opened_time = time.time()
            logging.info(f"Successfully opened stream on device {device_index}.")
            logging.info(f"  Time to open stream: {stream_opened_time - stream_start_time:.3f} seconds")

        except OSError as e:
            logging.error(f"!!!!!!!! FAILED TO OPEN STREAM on device {device_index} !!!!!!!!")
            logging.error(f"  Error: {e}")
            logging.error("  Common Causes:")
            logging.error("    - Incorrect device index.")
            logging.error("    - Device is busy or unavailable (check `alsamixer`, `pulseaudio`, etc.).")
            logging.error(f"    - Device does *not* support the requested format ({target_rate} Hz, {target_channels} Ch, {target_bit_depth}-bit).")
            logging.error("    - Insufficient permissions.")
            return False
        except Exception as e:
            logging.error(f"Unexpected error opening stream: {e}")
            return False


        # --- 6. Playback Loop ---
        logging.info("Starting playback loop...")
        data = wf.readframes(chunk_size)
        playback_start_time = time.time()
        frames_written = 0
        io_warnings = 0

        while len(data) > 0:
            try:
                # NOTE: If target_channels != native_channels, the data might need manipulation
                #       before writing (e.g., duplicating mono to stereo, or mixing stereo to mono).
                #       This basic script DOES NOT perform such manipulation. It sends the raw
                #       WAV data assuming the channel count matches what the stream expects.
                #       Mismatch here could lead to garbled audio or errors depending on driver.
                stream.write(data)
                # Calculate frames based on NATIVE properties, as that's what wf.readframes uses
                frames_written += len(data) // (native_channels * native_width)
                total_frames_read += chunk_size # Assume we read a full chunk initially
                data = wf.readframes(chunk_size)
            except IOError as e:
                logging.warning(f"IOError during playback: {e}. Check system load or try increasing chunk size.")
                io_warnings += 1
            except Exception as e:
                logging.error(f"Unexpected error during stream write: {e}")
                break

        # Get actual frames read accurately
        total_frames_read = wf.tell()

        logging.info("Waiting for stream buffer to empty...")
        stream_finish_wait_start = time.time()
        stream.stop_stream()
        playback_end_time = time.time()
        logging.info(f"Stream finished processing in {playback_end_time - stream_finish_wait_start:.3f} seconds.")

        actual_playback_duration = playback_end_time - playback_start_time
        logging.info("-" * 40)
        logging.info("Playback Loop Finished.")
        logging.info(f"  Total frames read from file: {total_frames_read}")
        logging.info(f"  IO Warnings during playback: {io_warnings}")
        logging.info(f"  Actual playback duration (write loop + buffer drain): {actual_playback_duration:.2f} seconds")

        # --- Timing Analysis ---
        if native_rate > 0:
            expected_duration_played = total_frames_read / float(native_rate)
            logging.info(f"  Expected duration for frames read (at native rate): {expected_duration_played:.2f} seconds")
            if expected_duration_played > 0.01:
                speed_ratio = actual_playback_duration / expected_duration_played
                logging.info(f"  Playback Speed Ratio (Actual Duration / Expected Duration): {speed_ratio:.3f}")
                if abs(speed_ratio - 1.0) > 0.05:
                    logging.warning("  -> Playback speed appears significantly different from normal (Ratio != 1.0).")
                    if native_rate != target_rate:
                         logging.warning("     This strongly correlates with the SAMPLE RATE MISMATCH noted earlier.")
                    else:
                         logging.warning("     If rates match, check system load, buffer issues (try changing chunk size), or ALSA/PulseAudio configuration.")
                else:
                    logging.info("  -> Playback speed appears normal.")
            else:
                 logging.info("  -> Too little audio data to reliably calculate speed ratio.")
        else:
            logging.warning("  Could not calculate expected duration (native rate unknown or zero).")

        success = True

    except Exception as e:
        logging.exception(f"An unexpected error occurred during the playback process: {e}")
        success = False

    finally:
        # --- 7. Cleanup ---
        logging.info("Cleaning up resources...")
        if stream is not None:
            try:
                if not stream.is_stopped():
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

    logging.info(f"--- Playback Test {'Completed' if success else 'Failed'} ---")
    return success

def get_int_input(prompt, min_val=None, max_val=None):
    """Helper function to get validated integer input."""
    while True:
        try:
            value_str = input(prompt)
            value = int(value_str)
            if min_val is not None and value < min_val:
                print(f"Error: Value must be at least {min_val}.")
            elif max_val is not None and value > max_val:
                 print(f"Error: Value must be no more than {max_val}.")
            else:
                return value
        except ValueError:
            print("Invalid input. Please enter a whole number.")
        except EOFError:
            logging.warning("\nInput stream ended. Exiting.")
            sys.exit(1)

def get_validated_filepath(prompt):
     """Helper function to get a valid WAV file path."""
     while True:
         try:
            filepath = input(prompt).strip()
            if not filepath:
                print("Error: File path cannot be empty.")
                continue
            if not filepath.lower().endswith(".wav"):
                print("Error: File must have a .wav extension.")
                continue
            if not os.path.exists(filepath):
                print(f"Error: File not found at '{filepath}'. Please check the path.")
                continue
            if not os.path.isfile(filepath):
                 print(f"Error: '{filepath}' is a directory, not a file.")
                 continue
            try:
                with open(filepath, 'rb') as f:
                    f.read(1) # Try reading one byte
                return filepath
            except IOError as e:
                 print(f"Error: Cannot read file '{filepath}'. Check permissions. ({e})")

         except EOFError:
             logging.warning("\nInput stream ended. Exiting.")
             sys.exit(1)


# ================================================
# Main Execution
# ================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="PyAudio WAV Playback Test Script with Optional Arguments.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "-f", "--file", type=str, help="Path to the WAV file to play."
    )
    parser.add_argument(
        "-d", "--device", type=int, help="Output device index (use --list to see devices)."
    )
    parser.add_argument(
        "-r", "--rate", type=int, help="Target sample rate (e.g., 44100, 48000, 96000)."
    )
    parser.add_argument(
        "-b", "--bits", type=int, choices=[16, 24], help="Target bit depth (16 or 24)."
    )
    # New argument for channels
    parser.add_argument(
        "-ch", "--channels", type=int, help="Target number of output channels (e.g., 1 for mono, 2 for stereo)."
    )
    parser.add_argument(
        "-c", "--chunk", type=int, default=DEFAULT_CHUNK_SIZE,
        help="Playback buffer chunk size (frames)."
    )
    parser.add_argument(
        "--list", action="store_true", help="List available audio devices and exit."
    )

    args = parser.parse_args()

    # --- List Devices and Exit ---
    if args.list:
        list_audio_devices()
        sys.exit(0)

    # --- Gather Configuration ---
    print("\n--- Audio Playback Configuration ---")
    list_audio_devices() # List devices first to help user choose

    # 1. WAV File Path
    wav_filepath = args.file
    if wav_filepath is None:
        print("\nNo WAV file specified via --file argument.")
        wav_filepath = get_validated_filepath("Enter the full path to the WAV file: ")
    else:
        if not wav_filepath.lower().endswith(".wav"):
             logging.error(f"Error: Specified file '{wav_filepath}' does not end with .wav")
             sys.exit(1)
        if not os.path.exists(wav_filepath):
            logging.error(f"Error: Specified file not found: '{wav_filepath}'")
            sys.exit(1)
        if not os.path.isfile(wav_filepath):
            logging.error(f"Error: Specified path is not a file: '{wav_filepath}'")
            sys.exit(1)
        logging.info(f"Using WAV file from argument: {wav_filepath}")


    # 2. Output Device Index
    output_device_index = args.device
    if output_device_index is None:
        print("\nNo output device index specified via --device argument.")
        output_device_index = get_int_input("Enter the target output device index (from list above): ", min_val=0)
    else:
        if output_device_index < 0:
             logging.error("Error: Device index cannot be negative.")
             sys.exit(1)
        logging.info(f"Using device index from argument: {output_device_index}")


    # 3. Target Sample Rate
    target_rate = args.rate
    if target_rate is None:
        print("\nNo target sample rate specified via --rate argument.")
        target_rate = get_int_input("Enter the target sample rate in Hz (e.g., 44100, 48000): ", min_val=1000)
    else:
         if target_rate <= 0:
              logging.error("Error: Sample rate must be positive.")
              sys.exit(1)
         logging.info(f"Using target sample rate from argument: {target_rate} Hz")


    # 4. Target Bit Depth
    target_bit_depth = args.bits
    if target_bit_depth is None:
         print("\nNo target bit depth specified via --bits argument.")
         while True:
             depth = get_int_input("Enter the target bit depth (16 or 24): ")
             if depth in [16, 24]:
                 target_bit_depth = depth
                 break
             else:
                 print("Error: Bit depth must be 16 or 24.")
    else:
         logging.info(f"Using target bit depth from argument: {target_bit_depth}-bit")

    # 5. Target Channels (New)
    target_channels = args.channels
    if target_channels is None:
        print("\nNo target channel count specified via --channels argument.")
        # Suggest common values
        target_channels = get_int_input("Enter the target number of channels (e.g., 1=Mono, 2=Stereo): ", min_val=1)
    else:
        if target_channels <= 0:
            logging.error("Error: Number of channels must be positive.")
            sys.exit(1)
        logging.info(f"Using target channel count from argument: {target_channels}")


    # 6. Chunk Size
    chunk_size = args.chunk
    if chunk_size <= 0:
        logging.error(f"Error: Chunk size (--chunk {chunk_size}) must be positive.")
        sys.exit(1)
    if args.chunk != DEFAULT_CHUNK_SIZE:
        logging.info(f"Using chunk size from argument: {chunk_size}")
    else:
        logging.info(f"Using default chunk size: {chunk_size}")


    print("\n" + "=" * 60)
    logging.info("Final Configuration to be Tested:")
    logging.info(f"  WAV File: '{wav_filepath}'")
    logging.info(f"  Output Device Index: {output_device_index}")
    logging.info(f"  Target Rate: {target_rate} Hz")
    logging.info(f"  Target Bit Depth: {target_bit_depth}-bit")
    logging.info(f"  Target Channels: {target_channels}") # Log final channels
    logging.info(f"  Chunk Size: {chunk_size}")
    print("=" * 60 + "\n")

    try:
        confirm = input("Press Enter to start the test, or Ctrl+C to abort...")
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(0)
    except EOFError:
        logging.warning("\nInput stream ended. Exiting.")
        sys.exit(1)


    # --- Run the playback test ---
    # Pass target_channels to the function
    play_test_audio(
        filepath=wav_filepath,
        device_index=output_device_index,
        target_rate=target_rate,
        target_bit_depth=target_bit_depth,
        target_channels=target_channels, # Pass the value
        chunk_size=chunk_size
    )

    print("\nScript finished.")