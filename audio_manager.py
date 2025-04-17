#!/usr/bin/env python3
"""
Save, convert and play audio – now using config.DAC_APLAY_DEVICE.
"""

import os
import sys
import subprocess
import logging
from pydub import AudioSegment

from config import DAC_APLAY_DEVICE

log = logging.getLogger(__name__)


# ------------------------------------------------------------------#
#  Helpers                                                          #
# ------------------------------------------------------------------#
def save_stream_to_file(stream, filepath):
    """Save streaming data to a file."""
    with open(filepath, "wb") as f:
        for chunk in stream:
            f.write(chunk)
    log.info("File saved: %s", filepath)


def convert_mp3_to_wav(mp3_filepath, wav_filepath):
    """Convert MP3 → WAV, 44.1 kHz stereo 16‑bit."""
    audio = AudioSegment.from_mp3(mp3_filepath)
    audio = audio.set_frame_rate(44100).set_sample_width(2).set_channels(2)
    audio.export(wav_filepath, format="wav")
    log.info("Converted %s → %s", mp3_filepath, wav_filepath)


def play_audio(filepath):
    """Play audio using system command."""
    if sys.platform.startswith("linux"):
        cmd = ["aplay", "-D", DAC_APLAY_DEVICE, filepath]
    elif sys.platform == "darwin":
        cmd = ["afplay", filepath]
    else:
        cmd = ["aplay", filepath]
    log.info("Playing: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)
