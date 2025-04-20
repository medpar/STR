# config.py

#!/usr/bin/env python3
"""
Centralised STR hardware/audio/GPIO settings (plus vector store and AI model config).
"""

import os
import logging

# Helper to find device index (run this script directly if needed)
def find_device_index(p, device_name_part, is_input=True):
    """Utility to find a device index containing a name part."""
    target_field = 'maxInputChannels' if is_input else 'maxOutputChannels'
    num_devices = p.get_device_count()
    for i in range(num_devices):
        info = p.get_device_info_by_index(i)
        if info[target_field] > 0 and device_name_part.lower() in info['name'].lower():
            return i
    return None

# Temporarily initialize PyAudio to find devices if needed for defaults
try:
    import pyaudio
    p = pyaudio.PyAudio()
    DEFAULT_MIC_INDEX = find_device_index(p, 'usb', is_input=True) or 1 # Default guess
    # Try finding common DAC names like 'pcm5102' or 'audioinjector' or just use default output
    DEFAULT_DAC_INDEX = find_device_index(p, 'pcm5102', is_input=False) or \
                        find_device_index(p, 'audioinjector', is_input=False) or \
                        find_device_index(p, 'speaker', is_input=False) or \
                        p.get_default_output_device_info()['index'] or 1 # Default guess
    p.terminate()
except Exception as e:
    logging.warning(f"PyAudio check failed during config load: {e}. Using fallback defaults.")
    DEFAULT_MIC_INDEX = 1
    DEFAULT_DAC_INDEX = 1


# ------------------------------------------------------------------#
# Audio (USB mic)
# ------------------------------------------------------------------#
MIC_DEVICE_INDEX: int = int(os.getenv("MIC_DEVICE_INDEX", str(DEFAULT_MIC_INDEX)))
# Let PyAudio determine optimal sample rate if not set (0)
MIC_SAMPLE_RATE: int  = int(os.getenv("MIC_SAMPLE_RATE", "0"))
MIC_CHANNELS: int     = int(os.getenv("MIC_CHANNELS",    "1"))
MIC_CHUNK: int        = int(os.getenv("MIC_CHUNK",      "1024"))
MIC_NORMALISE: bool   = os.getenv("MIC_NORMALISE",      "1") == "1"

# ------------------------------------------------------------------#
# Playback (PCM5102 DAC via PyAudio)
# ------------------------------------------------------------------#
# Replace DAC_APLAY_DEVICE with DAC_PYAUDIO_INDEX
# IMPORTANT: You might need to adjust this index based on your system.
# Run `python -m sounddevice` or a similar tool to list audio devices and find the correct index for your DAC.
DAC_PYAUDIO_INDEX: int = int(os.getenv("DAC_PYAUDIO_INDEX", str(DEFAULT_DAC_INDEX)))
PLAYBACK_CHUNK: int = 1024 # Chunk size for playback


# ------------------------------------------------------------------#
# GPIO – push‑button + LED
# ------------------------------------------------------------------#
GPIO_BUTTON_PIN: int  = int(os.getenv("GPIO_BUTTON_PIN", "17"))
GPIO_LED_PIN:   int   = int(os.getenv("GPIO_LED_PIN",    "27"))
# Set to True if button connects GPIO to 3.3V when pressed, False if it connects to GND.
# Depends on your wiring (pull-up vs pull-down resistor).
BUTTON_ACTIVE_HIGH: bool = os.getenv("BUTTON_ACTIVE_HIGH", "False").lower() in ("true", "1", "yes")
# Enable GPIO functionality (set to False to disable even if RPi.GPIO is installed)
ENABLE_GPIO: bool = os.getenv("ENABLE_GPIO", "True").lower() in ("true", "1", "yes")

# ------------------------------------------------------------------#
# OpenAI Vector Store for File Search
# Set VECTOR_STORE_ID to your existing vector store in ENV
# ------------------------------------------------------------------#
VECTOR_STORE_ID: str = os.getenv("VECTOR_STORE_ID", "")
if not VECTOR_STORE_ID:
    # Allow running without vector store for basic functionality
    logging.warning("VECTOR_STORE_ID not set in environment. PDF features will fail.")
    # raise RuntimeError("Please set VECTOR_STORE_ID in your environment") # Optional: make it mandatory

# ------------------------------------------------------------------#
# OpenAI Model Configuration
# You can override these via environment variables.
# ------------------------------------------------------------------#
OPENAI_MODEL_FILE_QA: str       = os.getenv("OPENAI_MODEL_FILE_QA",       "gpt-4.1-mini")
OPENAI_MODEL_AGENT: str         = os.getenv("OPENAI_MODEL_AGENT",         "gpt-4.1-mini")
OPENAI_MODEL_REALTIME: str      = os.getenv("OPENAI_MODEL_REALTIME",      "gpt-4o-mini-realtime-preview")
OPENAI_MODEL_TRANSCRIPTION: str = os.getenv("OPENAI_MODEL_TRANSCRIPTION", "whisper-1")


# Log key config values on startup
logging.info(f"--- Configuration ---")
logging.info(f"Mic Device Index: {MIC_DEVICE_INDEX}")
logging.info(f"DAC Device Index: {DAC_PYAUDIO_INDEX}")
logging.info(f"GPIO Enabled: {ENABLE_GPIO}")
if ENABLE_GPIO:
    logging.info(f"  Button Pin: {GPIO_BUTTON_PIN} (Active High: {BUTTON_ACTIVE_HIGH})")
    logging.info(f"  LED Pin: {GPIO_LED_PIN}")
logging.info(f"Vector Store ID: {'Set' if VECTOR_STORE_ID else 'Not Set'}")
logging.info(f"--------------------")

# Add a check for finding device indices if run directly
if __name__ == "__main__":
    print("--- Audio Device List ---")
    try:
        import pyaudio
        p = pyaudio.PyAudio()
        print(f"Default Input Index: {p.get_default_input_device_info()['index']}")
        print(f"Default Output Index: {p.get_default_output_device_info()['index']}")
        print("-" * 25)
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            print(f"Index {i}: {info['name']} (In: {info['maxInputChannels']}, Out: {info['maxOutputChannels']})")
        p.terminate()
        print("-" * 25)
        print(f"Configured MIC Index: {MIC_DEVICE_INDEX}")
        print(f"Configured DAC Index: {DAC_PYAUDIO_INDEX}")
    except Exception as e:
        print(f"Could not list audio devices: {e}")
    print("-------------------------")