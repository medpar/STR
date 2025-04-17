#!/usr/bin/env python3
"""
Centralised hardware / audio settings so you only change them once.

Override any of these with environment variables in `.env`.
"""

import os

# ------------------------------------------------------------#
#  Audio                                                      #
# ------------------------------------------------------------#
MIC_DEVICE_INDEX: int = int(os.getenv("MIC_DEVICE_INDEX", "1"))
SAMPLE_RATE: int = int(os.getenv("MIC_SAMPLE_RATE", "24000"))   # 24000, 44100, 48000 …
NUM_CHANNELS: int = int(os.getenv("MIC_CHANNELS", "1"))
FRAME_CHUNK: int = int(os.getenv("MIC_CHUNK", "1024"))

# Normalise input audio to 90 % full‑scale to boost quiet mics
NORMALISE_INPUT: bool = os.getenv("MIC_NORMALISE", "1") == "1"

# Speaker / DAC device for `aplay`
DAC_APLAY_DEVICE: str = os.getenv("DAC_APLAY_DEVICE", "plughw:2,0")

# ------------------------------------------------------------#
#  GPIO (only used on Raspberry Pi)                            #
# ------------------------------------------------------------#
GPIO_BUTTON_PIN: int = int(os.getenv("GPIO_BUTTON_PIN", "17"))  # BCM numbering
GPIO_LED_PIN: int    = int(os.getenv("GPIO_LED_PIN", "27"))
