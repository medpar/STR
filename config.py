# config.py

#!/usr/bin/env python3
"""
Centralised STR hardware/audio/GPIO settings (plus vector store and AI model config).
"""

import os

# ------------------------------------------------------------------#
# Audio (USB mic)
# ------------------------------------------------------------------#

MIC_DEVICE_INDEX: int = int(os.getenv("MIC_DEVICE_INDEX", "1"))
MIC_SAMPLE_RATE: int  = int(os.getenv("MIC_SAMPLE_RATE", "0"))
MIC_CHANNELS: int     = int(os.getenv("MIC_CHANNELS",    "1"))
MIC_CHUNK: int        = int(os.getenv("MIC_CHUNK",      "1024"))
MIC_NORMALISE: bool   = os.getenv("MIC_NORMALISE",      "1") == "1"

# ------------------------------------------------------------------#
# Playback (PCM5102 DAC via aplay)
# ------------------------------------------------------------------#

DAC_APLAY_DEVICE: str = os.getenv("DAC_APLAY_DEVICE", "plughw:1,0")

# ------------------------------------------------------------------#
# #GPIO – push‑button + LED
# ------------------------------------------------------------------#

GPIO_BUTTON_PIN: int  = int(os.getenv("GPIO_BUTTON_PIN", "17"))
GPIO_LED_PIN:   int   = int(os.getenv("GPIO_LED_PIN",    "27"))
BUTTON_ACTIVE_HIGH: bool = True

# ------------------------------------------------------------------#
# OpenAI Vector Store for File Search
# Set VECTOR_STORE_ID to your existing vector store in ENV
# ------------------------------------------------------------------#

VECTOR_STORE_ID: str = os.getenv("VECTOR_STORE_ID", "")
if not VECTOR_STORE_ID:
    raise RuntimeError("Please set VECTOR_STORE_ID in your environment")

# ------------------------------------------------------------------#
# OpenAI Model Configuration
# You can override these via environment variables.
# ------------------------------------------------------------------#

OPENAI_MODEL_FILE_QA: str       = os.getenv("OPENAI_MODEL_FILE_QA",       "gpt-4.1-mini")
OPENAI_MODEL_AGENT: str         = os.getenv("OPENAI_MODEL_AGENT",         "gpt-4.1-mini")
OPENAI_MODEL_REALTIME: str      = os.getenv("OPENAI_MODEL_REALTIME",      "gpt-4o-mini-realtime-preview")
OPENAI_MODEL_TRANSCRIPTION: str = os.getenv("OPENAI_MODEL_TRANSCRIPTION", "whisper-1")
