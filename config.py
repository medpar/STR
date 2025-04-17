#!/usr/bin/env python3
"""
Centralised STR hardware/audio/GPIO settings.

Edit this file (or override with ENV vars) whenever the USB‑mic or DAC
IDs change – no need to touch the rest of the code.
"""

import os

# ------------------------------------------------------------------#
#  Audio (USB mic)                                                  #
# ------------------------------------------------------------------#
MIC_DEVICE_INDEX: int = int(os.getenv("MIC_DEVICE_INDEX", "1"))

# Set to 0 to auto‑query the mic’s native rate each time
MIC_SAMPLE_RATE: int = int(os.getenv("MIC_SAMPLE_RATE", "0"))

MIC_CHANNELS: int = int(os.getenv("MIC_CHANNELS", "1"))
MIC_CHUNK: int = int(os.getenv("MIC_CHUNK", "1024"))
MIC_NORMALISE: bool = os.getenv("MIC_NORMALISE", "1") == "1"

# ------------------------------------------------------------------#
#  Playback (PCM5102 DAC via aplay)                                 #
# ------------------------------------------------------------------#
DAC_APLAY_DEVICE: str = os.getenv("DAC_APLAY_DEVICE", "plughw:2,0")

# ------------------------------------------------------------------#
#  GPIO – push‑button + LED                                         #
# ------------------------------------------------------------------#
GPIO_BUTTON_PIN: int = int(os.getenv("GPIO_BUTTON_PIN", "17"))   # BCM
GPIO_LED_PIN: int    = int(os.getenv("GPIO_LED_PIN", "27"))

# External resistor pull‑down (True = button pulls GPIO *high* when pressed)
BUTTON_ACTIVE_HIGH: bool = True
