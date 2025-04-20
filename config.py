#!/usr/bin/env python3
"""
Centralised STR hardware/audio/GPIO settings (plus vector store and AI model config).
"""

import os
import logging
import sys

# --- Logging Setup ---
# Configure logging early to capture potential issues during config load
log_config = logging.getLogger("config")
# Keep basicConfig for other modules that might not configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)5s | %(name)s | %(message)s")

# Helper to find device index (run this script directly if needed)
def find_device_index(p, device_name_part, is_input=True):
    """Utility to find a device index containing a name part."""
    target_field = 'max_input_channels' if is_input else 'max_output_channels'
    num_devices = p.get_device_count()
    log_config.debug(f"Searching for {'input' if is_input else 'output'} device containing '{device_name_part}'. Found {num_devices} devices.")
    for i in range(num_devices):
        try:
            info = p.get_device_info_by_index(i)
            log_config.debug(f"  Device {i}: {info.get('name')}, {target_field}: {info.get(target_field)}")
            # Updated key names for newer PyAudio/PortAudio versions
            if info.get(target_field, 0) > 0 and device_name_part.lower() in info.get('name', '').lower():
                log_config.debug(f"  -> Found match at index {i}")
                return i
        except Exception as e:
            log_config.warning(f"Could not query device index {i}: {e}")
    log_config.debug(f"  -> No match found for '{device_name_part}'.")
    return None

# Temporarily initialize PyAudio to find devices if needed for defaults
DEFAULT_MIC_INDEX = 1  # Fallback default
DEFAULT_DAC_INDEX = 1  # Fallback default
detected_mic_name = "Not detected"
detected_dac_name = "Not detected / Default"

try:
    import pyaudio
    p = pyaudio.PyAudio()

    # --- Microphone Detection ---
    # Prioritize USB mics if present
    found_mic_index = find_device_index(p, 'usb', is_input=True)
    if found_mic_index is None:
        # Fallback to finding any potential mic names
        common_mic_names = ['mic', 'input', 'capture', 'default']
        for name in common_mic_names:
             found_mic_index = find_device_index(p, name, is_input=True)
             if found_mic_index is not None:
                 break

    if found_mic_index is not None:
        DEFAULT_MIC_INDEX = found_mic_index
        try:
            detected_mic_name = p.get_device_info_by_index(DEFAULT_MIC_INDEX)['name']
        except Exception:
            detected_mic_name = f"Detected Index {DEFAULT_MIC_INDEX} (name error)"
    else:
        try:
            # If no specific mic found, use PyAudio's default input
            default_input_info = p.get_default_input_device_info()
            DEFAULT_MIC_INDEX = default_input_info['index']
            detected_mic_name = f"PyAudio Default Input ({default_input_info['name']})"
        except Exception as e:
            log_config.warning(f"PyAudio couldn't find default input device: {e}. Using fallback index {DEFAULT_MIC_INDEX}.")
            detected_mic_name = f"Fallback Index {DEFAULT_MIC_INDEX}"


    # --- DAC/Output Detection ---
    # Prioritize specific known DAC names for RPi
    dac_search_terms = ['pcm5102', 'audioinjector', 'hifiberry', 'speaker', 'usb audio', 'dac']
    found_dac_index = None
    for term in dac_search_terms:
        found_dac_index = find_device_index(p, term, is_input=False)
        if found_dac_index is not None:
            break

    if found_dac_index is not None:
        DEFAULT_DAC_INDEX = found_dac_index
        try:
            detected_dac_name = p.get_device_info_by_index(DEFAULT_DAC_INDEX)['name']
        except Exception:
            detected_dac_name = f"Detected Index {DEFAULT_DAC_INDEX} (name error)"
    else:
        try:
            # If no specific DAC found, use PyAudio's default output
            default_output_info = p.get_default_output_device_info()
            DEFAULT_DAC_INDEX = default_output_info['index']
            detected_dac_name = f"PyAudio Default Output ({default_output_info['name']})"
        except Exception as e:
            log_config.warning(f"PyAudio couldn't find default output device: {e}. Using fallback index {DEFAULT_DAC_INDEX}.")
            detected_dac_name = f"Fallback Index {DEFAULT_DAC_INDEX}"

    p.terminate()
except Exception as e:
    log_config.warning(f"PyAudio check failed during config load: {e}. Using fallback default indices (Mic: {DEFAULT_MIC_INDEX}, DAC: {DEFAULT_DAC_INDEX}).")
    detected_mic_name = f"Fallback Index {DEFAULT_MIC_INDEX} (PyAudio Error)"
    detected_dac_name = f"Fallback Index {DEFAULT_DAC_INDEX} (PyAudio Error)"


# ------------------------------------------------------------------#
# Audio (USB mic)
# ------------------------------------------------------------------#
# Use environment variable first, then detected default, finally hardcoded fallback
MIC_DEVICE_INDEX: int = int(os.getenv("MIC_DEVICE_INDEX", str(DEFAULT_MIC_INDEX)))
MIC_SAMPLE_RATE: int  = int(os.getenv("MIC_SAMPLE_RATE", "0")) # 0 lets PyAudio choose default rate
MIC_CHANNELS: int     = int(os.getenv("MIC_CHANNELS",    "1"))
MIC_CHUNK: int        = int(os.getenv("MIC_CHUNK",      "1024"))
MIC_NORMALISE: bool   = os.getenv("MIC_NORMALISE",      "1") == "1"

# ------------------------------------------------------------------#
# Playback (PCM5102 DAC via PyAudio)
# ------------------------------------------------------------------#
# Use environment variable first, then detected default, finally hardcoded fallback
# *** IMPORTANT: Set the DAC_PYAUDIO_INDEX environment variable to the correct index ***
# *** for your PCM5102 DAC on the Raspberry Pi. Use 'python -m sounddevice' to find it. ***
DAC_PYAUDIO_INDEX: int = int(os.getenv("DAC_PYAUDIO_INDEX", str(DEFAULT_DAC_INDEX)))
PLAYBACK_CHUNK: int    = 1024  # chunk size
# Force all playback to this DAC rate so things never sound slow/fast
# 48000 is a common rate for PCM5102 and many USB DACs. 44100 is also common.
OUTPUT_SAMPLE_RATE: int = int(os.getenv("OUTPUT_SAMPLE_RATE", "48000"))


# ------------------------------------------------------------------#
# GPIO – push‑button + LED
# ------------------------------------------------------------------#
GPIO_BUTTON_PIN: int  = int(os.getenv("GPIO_BUTTON_PIN", "17"))
GPIO_LED_PIN:   int   = int(os.getenv("GPIO_LED_PIN",    "27"))
BUTTON_ACTIVE_HIGH: bool = os.getenv("BUTTON_ACTIVE_HIGH", "False").lower() in ("true", "1", "yes")
# Automatically disable GPIO if not on RPi or if explicitly disabled
try:
    import RPi.GPIO
    _IS_RPI = True
except (ImportError, RuntimeError):
    _IS_RPI = False

ENABLE_GPIO_ENV: bool = os.getenv("ENABLE_GPIO", "True").lower() in ("true", "1", "yes")
ENABLE_GPIO: bool = ENABLE_GPIO_ENV and _IS_RPI


# ------------------------------------------------------------------#
# OpenAI Vector Store for File Search
# ------------------------------------------------------------------#
VECTOR_STORE_ID: str = os.getenv("VECTOR_STORE_ID", "")
if not VECTOR_STORE_ID:
    log_config.warning("VECTOR_STORE_ID not set in environment. PDF features will fail.")


# ------------------------------------------------------------------#
# OpenAI Model Configuration
# ------------------------------------------------------------------#
OPENAI_MODEL_FILE_QA: str       = os.getenv("OPENAI_MODEL_FILE_QA",       "gpt-4o-mini") # Updated default
OPENAI_MODEL_AGENT: str         = os.getenv("OPENAI_MODEL_AGENT",         "gpt-4o-mini") # Updated default
# Use standard gpt-4o-mini for realtime if preview isn't needed or causes issues
OPENAI_MODEL_REALTIME: str      = os.getenv("OPENAI_MODEL_REALTIME",      "gpt-4o-mini")
OPENAI_MODEL_TRANSCRIPTION: str = os.getenv("OPENAI_MODEL_TRANSCRIPTION", "whisper-1")


# Log key config values on startup
log_config.info("--- Configuration ---")
log_config.info(f"Platform: {'Raspberry Pi' if _IS_RPI else sys.platform}")
log_config.info(f"Detected Mic Device: '{detected_mic_name}' (Using Index: {MIC_DEVICE_INDEX})")
log_config.info(f"Detected DAC/Output Device: '{detected_dac_name}' (Using Index: {DAC_PYAUDIO_INDEX})")
log_config.info(f" --> IMPORTANT: For RPi DAC, ensure DAC_PYAUDIO_INDEX is correct (use env var if needed).")
log_config.info(f"Output Sample Rate: {OUTPUT_SAMPLE_RATE} Hz")
log_config.info(f"GPIO Enabled: {ENABLE_GPIO}")
if ENABLE_GPIO:
    log_config.info(f"  Button Pin: {GPIO_BUTTON_PIN} (Active High: {BUTTON_ACTIVE_HIGH})")
    log_config.info(f"  LED Pin: {GPIO_LED_PIN}")
else:
    if not _IS_RPI:
        log_config.info("  (GPIO disabled: Not running on Raspberry Pi)")
    elif not ENABLE_GPIO_ENV:
        log_config.info("  (GPIO disabled: ENABLE_GPIO environment variable is not True)")
log_config.info(f"Vector Store ID: {'Set' if VECTOR_STORE_ID else 'Not Set'}")
log_config.info(f"OpenAI Models: QA={OPENAI_MODEL_FILE_QA}, Agent={OPENAI_MODEL_AGENT}, Realtime={OPENAI_MODEL_REALTIME}")
log_config.info("--------------------")