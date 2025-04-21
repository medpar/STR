#!/usr/bin/env python3
"""
Centralised STR hardware/audio/GPIO settings (plus vector store and AI model config).
Focuses on robust audio device detection.
"""

import os
import logging
import sys

# --- Logging Setup ---
log_config = logging.getLogger("config")
# Ensure handler is added if not already configured by main app
if not log_config.hasHandlers():
    log_config.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(asctime)s | %(levelname)5s | %(name)s | %(message)s')
    handler.setFormatter(formatter)
    log_config.addHandler(handler)
    log_config.propagate = False # Prevent duplicate messages if root logger also has handler
else:
     # If already configured (e.g., by app.py basicConfig), ensure level is appropriate
     log_config.setLevel(logging.INFO)


# --- Device Detection Helper ---
def find_device_by_name_fragment(p, name_fragments, is_input=True, threshold=0):
    """
    Finds the first device index matching any fragment in the list.

    Args:
        p (pyaudio.PyAudio): PyAudio instance.
        name_fragments (list[str]): List of lowercase name fragments to search for.
        is_input (bool): True to search for input devices, False for output.
        threshold (int): Minimum number of channels required (e.g., > 0).

    Returns:
        tuple(int, str) | None: (index, name) of the found device, or None.
    """
    target_field = 'maxInputChannels' if is_input else 'maxOutputChannels'
    num_devices = p.get_device_count()
    log_config.debug(f"Searching for {'input' if is_input else 'output'} device matching {name_fragments} ({num_devices} total devices).")
    for i in range(num_devices):
        try:
            info = p.get_device_info_by_index(i)
            device_name = info.get('name', '').lower()
            channels = info.get(target_field, 0)

            log_config.debug(f"  Checking Dev {i}: '{info.get('name', 'N/A')}', {target_field}={channels}")

            # Check channel count first (allow 0 for threshold)
            if channels >= threshold:
                # Check if any fragment matches the device name
                for fragment in name_fragments:
                    if fragment in device_name:
                        log_config.debug(f"  --> Match found for '{fragment}' at index {i}: '{info.get('name')}'")
                        return i, info.get('name') # Return index and actual name

        except Exception as e:
            log_config.warning(f"Could not query device index {i}: {e}")

    log_config.debug(f"  --> No suitable device found matching fragments: {name_fragments}.")
    return None


# --- Default Indices and Names ---
DEFAULT_MIC_INDEX = 1  # Fallback default
DEFAULT_DAC_INDEX = 0  # Fallback default (often built-in audio like headphones)
detected_mic_name = "Not detected (Using Fallback)"
detected_dac_name = "Not detected (Using Fallback)"
mic_detection_method = "Fallback"
dac_detection_method = "Fallback"

# --- Environment Variable Override Check ---
# Check if indices are explicitly set via environment variables
ENV_MIC_INDEX = os.getenv("MIC_DEVICE_INDEX")
ENV_DAC_INDEX = os.getenv("DAC_PYAUDIO_INDEX")

final_mic_index = None
final_dac_index = None

# --- PyAudio Device Detection ---
try:
    import pyaudio
    p = pyaudio.PyAudio()

    # --- Microphone Detection Logic ---
    if ENV_MIC_INDEX is not None:
        try:
            final_mic_index = int(ENV_MIC_INDEX)
            mic_info = p.get_device_info_by_index(final_mic_index)
            # Basic check if it's an input device
            if mic_info.get('maxInputChannels', 0) > 0:
                 detected_mic_name = mic_info.get('name', f"Index {final_mic_index}")
                 mic_detection_method = "Environment Variable"
                 log_config.info(f"Using MIC_DEVICE_INDEX from environment: {final_mic_index} ('{detected_mic_name}')")
            else:
                 log_config.warning(f"MIC_DEVICE_INDEX {final_mic_index} from env var is not an input device. Reverting to auto-detection.")
                 final_mic_index = None # Force auto-detection
        except (ValueError, OSError, IndexError) as e:
            log_config.warning(f"Invalid MIC_DEVICE_INDEX '{ENV_MIC_INDEX}' from environment variable: {e}. Reverting to auto-detection.")
            final_mic_index = None # Force auto-detection

    if final_mic_index is None: # Proceed with auto-detection if env var not used or invalid
        mic_detection_method = "Auto-Detect"
        # 1. Try specific names first (e.g., USB mics)
        mic_result = find_device_by_name_fragment(p, ['usb', 'microphone'], is_input=True, threshold=1) # Require at least 1 channel
        if mic_result:
            final_mic_index, detected_mic_name = mic_result
            mic_detection_method += ": USB/Mic Name"
        else:
            # 2. Try PyAudio default input
            ### ad
            try:
                default_input_info = p.get_default_input_device_info()
                final_mic_index = default_input_info['index']
                detected_mic_name = default_input_info.get('name', f"Index {final_mic_index}")
                mic_detection_method += ": PyAudio Default Input"
            except Exception as e:
                # 3. Fallback to hardcoded default index
                log_config.warning(f"Could not get PyAudio default input device: {e}. Using fallback index {DEFAULT_MIC_INDEX}.")
                final_mic_index = DEFAULT_MIC_INDEX
                try: # Try to get name for fallback index
                    mic_info = p.get_device_info_by_index(final_mic_index)
                    # Check if fallback is actually an input device
                    if mic_info.get('maxInputChannels', 0) > 0:
                        detected_mic_name = mic_info.get('name', f"Index {final_mic_index}")
                    else:
                        detected_mic_name = f"Fallback Index {final_mic_index} (Not Input)"
                        log_config.error(f"Fallback Mic Index {final_mic_index} is not an input device!")
                except Exception:
                     detected_mic_name = f"Fallback Index {final_mic_index} (Name N/A)"
                mic_detection_method = "Fallback Index"


    # --- DAC/Output Detection Logic ---
    if ENV_DAC_INDEX is not None:
        try:
            final_dac_index = int(ENV_DAC_INDEX)
            dac_info = p.get_device_info_by_index(final_dac_index)
             # Basic check if it's an output device
            if dac_info.get('maxOutputChannels', 0) > 0:
                detected_dac_name = dac_info.get('name', f"Index {final_dac_index}")
                dac_detection_method = "Environment Variable"
                log_config.info(f"Using DAC_PYAUDIO_INDEX from environment: {final_dac_index} ('{detected_dac_name}')")
            else:
                log_config.warning(f"DAC_PYAUDIO_INDEX {final_dac_index} from env var is not an output device. Reverting to auto-detection.")
                final_dac_index = None # Force auto-detection
        except (ValueError, OSError, IndexError) as e:
            log_config.warning(f"Invalid DAC_PYAUDIO_INDEX '{ENV_DAC_INDEX}' from environment variable: {e}. Reverting to auto-detection.")
            final_dac_index = None # Force auto-detection

    if final_dac_index is None: # Proceed with auto-detection if env var not used or invalid
        dac_detection_method = "Auto-Detect"
        # Define search terms in priority order
        specific_rpi_dac_names = ['snd_rpi_hifiberry_dac', 'pcm5102', 'hifiberry', 'audioinjector']
        general_output_names = ['speaker', 'headphones', 'usb audio', 'dac'] # USB added here as lower priority

        # 1. Try specific RPi DAC names
        dac_result = find_device_by_name_fragment(p, specific_rpi_dac_names, is_input=False, threshold=1) # Require at least 1 output channel
        if dac_result:
            final_dac_index, detected_dac_name = dac_result
            dac_detection_method += ": Specific RPi DAC Name"
        else:
            # 2. Try general output names
            dac_result = find_device_by_name_fragment(p, general_output_names, is_input=False, threshold=1)
            if dac_result:
                final_dac_index, detected_dac_name = dac_result
                dac_detection_method += ": General Output Name"
            else:
                # 3. Try PyAudio default output
                try:
                    default_output_info = p.get_default_output_device_info()
                    final_dac_index = default_output_info['index']
                    detected_dac_name = default_output_info.get('name', f"Index {final_dac_index}")
                    dac_detection_method += ": PyAudio Default Output"
                except Exception as e:
                    # 4. Fallback to hardcoded default index
                    log_config.warning(f"Could not get PyAudio default output device: {e}. Using fallback index {DEFAULT_DAC_INDEX}.")
                    final_dac_index = DEFAULT_DAC_INDEX
                    try: # Try to get name for fallback index
                         dac_info = p.get_device_info_by_index(final_dac_index)
                         # Check if fallback is actually an output device
                         if dac_info.get('maxOutputChannels', 0) > 0:
                             detected_dac_name = dac_info.get('name', f"Index {final_dac_index}")
                         else:
                             detected_dac_name = f"Fallback Index {final_dac_index} (Not Output)"
                             log_config.error(f"Fallback DAC Index {final_dac_index} is not an output device!")
                    except Exception:
                         detected_dac_name = f"Fallback Index {final_dac_index} (Name N/A)"
                    dac_detection_method = "Fallback Index"

    p.terminate()
    log_config.debug("PyAudio terminated after device detection.")

except Exception as e:
    log_config.error(f"PyAudio check failed during config load: {e}. Audio features may not work.")
    # Use hardcoded fallbacks if PyAudio failed entirely
    final_mic_index = int(ENV_MIC_INDEX) if ENV_MIC_INDEX is not None else DEFAULT_MIC_INDEX
    final_dac_index = int(ENV_DAC_INDEX) if ENV_DAC_INDEX is not None else DEFAULT_DAC_INDEX
    mic_detection_method = "Error Fallback" + (": Env Var" if ENV_MIC_INDEX is not None else ": Hardcoded")
    dac_detection_method = "Error Fallback" + (": Env Var" if ENV_DAC_INDEX is not None else ": Hardcoded")
    detected_mic_name = f"Fallback Index {final_mic_index} (PyAudio Error)"
    detected_dac_name = f"Fallback Index {final_dac_index} (PyAudio Error)"


# ------------------------------------------------------------------#
# Audio (USB mic)
# ------------------------------------------------------------------#
MIC_DEVICE_INDEX: int = final_mic_index
MIC_SAMPLE_RATE: int  = int(os.getenv("MIC_SAMPLE_RATE", "0")) # 0 lets PyAudio choose default rate
MIC_CHANNELS: int     = int(os.getenv("MIC_CHANNELS",    "1"))
MIC_CHUNK: int        = int(os.getenv("MIC_CHUNK",      "1024"))
MIC_NORMALISE: bool   = os.getenv("MIC_NORMALISE",      "1") == "1"

# ------------------------------------------------------------------#
# Playback (PCM5102 DAC via PyAudio)
# ------------------------------------------------------------------#
DAC_PYAUDIO_INDEX: int = final_dac_index
PLAYBACK_CHUNK: int    = 1024  # chunk size
# *** CHANGE: Set default output rate to 44.1kHz ***
OUTPUT_SAMPLE_RATE: int = int(os.getenv("OUTPUT_SAMPLE_RATE", "64000")) # Default to 44.1kHz

# ------------------------------------------------------------------#
# GPIO – push‑button + LED
# ------------------------------------------------------------------#
GPIO_BUTTON_PIN: int  = int(os.getenv("GPIO_BUTTON_PIN", "17"))
GPIO_LED_PIN:   int   = int(os.getenv("GPIO_LED_PIN",    "27"))
BUTTON_ACTIVE_HIGH: bool = os.getenv("BUTTON_ACTIVE_HIGH", "True")
# Automatically disable GPIO if not on RPi or if explicitly disabled
_IS_RPI = False
if sys.platform == "linux":
     try:
          # Check for Raspberry Pi specific entries in cpuinfo
          with open('/proc/cpuinfo', 'r') as f:
               cpuinfo = f.read()
               if 'Raspberry Pi' in cpuinfo or 'BCM2708' in cpuinfo or 'BCM2709' in cpuinfo or 'BCM2835' in cpuinfo or 'BCM2836' in cpuinfo or 'BCM2837' in cpuinfo or 'BCM2711' in cpuinfo:
                   _IS_RPI = True

          if _IS_RPI:
              # Try importing RPi.GPIO only if detected as RPi
              import RPi.GPIO
     except (ImportError, RuntimeError, FileNotFoundError):
          _IS_RPI = False # Ensure it's False if check fails or RPi.GPIO not installed

ENABLE_GPIO_ENV: bool = os.getenv("ENABLE_GPIO", "True").lower() in ("true", "1", "yes")
ENABLE_GPIO: bool = ENABLE_GPIO_ENV and _IS_RPI

# ------------------------------------------------------------------#
# OpenAI Vector Store for File Search
# ------------------------------------------------------------------#
VECTOR_STORE_ID: str = os.getenv("VECTOR_STORE_ID", "vs_6800e568d74c8191927351dc5afbfd81")
if not VECTOR_STORE_ID:
    log_config.warning("VECTOR_STORE_ID not set in environment. PDF features will fail.")

# ------------------------------------------------------------------#
# OpenAI Model Configuration
# ------------------------------------------------------------------#
OPENAI_MODEL_FILE_QA: str       = os.getenv("OPENAI_MODEL_FILE_QA",       "gpt-4.1-mini")
OPENAI_MODEL_AGENT: str         = os.getenv("OPENAI_MODEL_AGENT",         "gpt-4.1-mini")
OPENAI_MODEL_REALTIME: str      = os.getenv("OPENAI_MODEL_REALTIME",      "gpt-4o-realtime-preview")
OPENAI_MODEL_TRANSCRIPTION: str = os.getenv("OPENAI_MODEL_TRANSCRIPTION", "whisper-1")


# --- Final Configuration Logging ---
log_config.info("--- Configuration ---")
log_config.info(f"Platform: {'Raspberry Pi' if _IS_RPI else sys.platform}")
log_config.info(f"Mic Device: Index={MIC_DEVICE_INDEX}, Name='{detected_mic_name}', Method='{mic_detection_method}'")
log_config.info(f"DAC Device: Index={DAC_PYAUDIO_INDEX}, Name='{detected_dac_name}', Method='{dac_detection_method}'")
if dac_detection_method == 'Auto-Detect: Specific RPi DAC Name':
    log_config.info(" --> Successfully detected specific RPi DAC.")
elif dac_detection_method == 'Environment Variable':
    log_config.info(" --> Using DAC index specified by DAC_PYAUDIO_INDEX environment variable.")
elif 'Fallback' in dac_detection_method or 'Default' in dac_detection_method:
    log_config.warning(" --> DAC detection fell back. Check PyAudio/ALSA setup if intended DAC wasn't found.")
    log_config.warning(" --> For RPi DAC, set DAC_PYAUDIO_INDEX environment variable for reliability.")

# Log the chosen output sample rate
log_config.info(f"Target Output Sample Rate: {OUTPUT_SAMPLE_RATE} Hz")
log_config.info(f"GPIO Enabled: {ENABLE_GPIO}")
if ENABLE_GPIO:
    log_config.info(f"  Button Pin: {GPIO_BUTTON_PIN} (Active High: {BUTTON_ACTIVE_HIGH})")
    log_config.info(f"  LED Pin: {GPIO_LED_PIN}")
else:
    if not _IS_RPI:
        log_config.info("  (GPIO disabled: Not detected as Raspberry Pi or RPi.GPIO missing)")
    elif not ENABLE_GPIO_ENV:
        log_config.info("  (GPIO disabled: ENABLE_GPIO environment variable is not True)")
log_config.info(f"Vector Store ID: {'Set' if VECTOR_STORE_ID else 'Not Set'}")
log_config.info(f"OpenAI Models: QA={OPENAI_MODEL_FILE_QA}, Agent={OPENAI_MODEL_AGENT}, Realtime={OPENAI_MODEL_REALTIME}")
log_config.info("--------------------")