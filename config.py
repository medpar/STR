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
    DEFAULT_MIC_INDEX = find_device_index(p, 'usb', is_input=True) or 1
    DEFAULT_DAC_INDEX = find_device_index(p, 'pcm5102', is_input=False) or \
                        find_device_index(p, 'audioinjector', is_input=False) or \
                        find_device_index(p, 'speaker', is_input=False) or \
                        p.get_default_output_device_info()['index'] or 1
    p.terminate()
except Exception as e:
    logging.warning(f"PyAudio check failed during config load: {e}. Using fallback defaults.")
    DEFAULT_MIC_INDEX = 1
    DEFAULT_DAC_INDEX = 1


# ------------------------------------------------------------------#
# Audio (USB mic)
# ------------------------------------------------------------------#
MIC_DEVICE_INDEX: int = int(os.getenv("MIC_DEVICE_INDEX", str(DEFAULT_MIC_INDEX)))
MIC_SAMPLE_RATE: int  = int(os.getenv("MIC_SAMPLE_RATE", "0"))  # 48 kHz mic
MIC_CHANNELS: int     = int(os.getenv("MIC_CHANNELS",    "1"))
MIC_CHUNK: int        = int(os.getenv("MIC_CHUNK",      "1024"))
MIC_NORMALISE: bool   = os.getenv("MIC_NORMALISE",      "1") == "1"

# ------------------------------------------------------------------#
# Playback (PCM5102 DAC via PyAudio)
# ------------------------------------------------------------------#
DAC_PYAUDIO_INDEX: int = int(os.getenv("DAC_PYAUDIO_INDEX", str(DEFAULT_DAC_INDEX)))
PLAYBACK_CHUNK: int    = 1024  # chunk size
# Force all playback to this DAC rate so things never sound slow
OUTPUT_SAMPLE_RATE: int = int(os.getenv("OUTPUT_SAMPLE_RATE", "48000"))


# ------------------------------------------------------------------#
# GPIO – push‑button + LED
# ------------------------------------------------------------------#
GPIO_BUTTON_PIN: int  = int(os.getenv("GPIO_BUTTON_PIN", "17"))
GPIO_LED_PIN:   int   = int(os.getenv("GPIO_LED_PIN",    "27"))
BUTTON_ACTIVE_HIGH: bool = os.getenv("BUTTON_ACTIVE_HIGH", "False").lower() in ("true", "1", "yes")
ENABLE_GPIO: bool       = os.getenv("ENABLE_GPIO", "True").lower() in ("true", "1", "yes")


# ------------------------------------------------------------------#
# OpenAI Vector Store for File Search
# ------------------------------------------------------------------#
VECTOR_STORE_ID: str = os.getenv("VECTOR_STORE_ID", "")
if not VECTOR_STORE_ID:
    logging.warning("VECTOR_STORE_ID not set in environment. PDF features will fail.")


# ------------------------------------------------------------------#
# OpenAI Model Configuration
# ------------------------------------------------------------------#
OPENAI_MODEL_FILE_QA: str       = os.getenv("OPENAI_MODEL_FILE_QA",       "gpt-4.1-mini")
OPENAI_MODEL_AGENT: str         = os.getenv("OPENAI_MODEL_AGENT",         "gpt-4.1-mini")
OPENAI_MODEL_REALTIME: str      = os.getenv("OPENAI_MODEL_REALTIME",      "gpt-4o-mini-realtime-preview")
OPENAI_MODEL_TRANSCRIPTION: str = os.getenv("OPENAI_MODEL_TRANSCRIPTION", "whisper-1")


# Log key config values on startup
logging.info(f"--- Configuration ---")
logging.info(f"Mic Device Index: {MIC_DEVICE_INDEX}")
logging.info(f"DAC Device Index: {DAC_PYAUDIO_INDEX}")
logging.info(f"Output Sample Rate: {OUTPUT_SAMPLE_RATE} Hz")
logging.info(f"GPIO Enabled: {ENABLE_GPIO}")
if ENABLE_GPIO:
    logging.info(f"  Button Pin: {GPIO_BUTTON_PIN} (Active High: {BUTTON_ACTIVE_HIGH})")
    logging.info(f"  LED Pin: {GPIO_LED_PIN}")
logging.info(f"Vector Store ID: {'Set' if VECTOR_STORE_ID else 'Not Set'}")
logging.info(f"--------------------")
