#!/usr/bin/env python3
"""
Save, convert and play audio.

● Converts MP3 → WAV at 44 100 Hz / 16‑bit / stereo (compatible with all ALSA plug‑ins).
● Plays WAV files through the system player (`aplay` on Linux, `afplay` on macOS).
   This bypasses the timing/pitch problems that arose with the PyAudio‑based
   player when driving the PCM5102; ALSA takes care of any extra resampling.

The external approach proved robust in tu setup anterior, so we restore it here.
No other modules need to change – `play_audio()` keeps the same signature, and
the stubbed PyAudio helpers satisfy `realtime.py`’s import of
`terminate_pyaudio_instance`.
"""

from __future__ import annotations
import os
import sys
import subprocess
import logging
from typing import Optional

from pydub import AudioSegment
from pydub.exceptions import CouldntDecodeError

log = logging.getLogger(__name__)

# ------------------------------------------------------------------#
# Optional: ALSA device name                                         #
#   • If you set the env‑var DAC_APLAY_DEVICE, we pass it to aplay.  #
#   • Otherwise ALSA will use its default PCM plug‑in.               #
# ------------------------------------------------------------------#
_DAC_DEVICE = os.getenv("DAC_APLAY_DEVICE", "default")

# ------------------------------------------------------------------#
# PyAudio helpers – kept as stubs so existing imports still work     #
# ------------------------------------------------------------------#
_pyaudio_instance: Optional[object] = None   # type: ignore


def _get_pyaudio_instance():
    """Deprecated: retained only to keep external API stable."""
    global _pyaudio_instance
    if _pyaudio_instance is None:
        try:
            import pyaudio  # local import to avoid requirement on non‑RPi hosts
            _pyaudio_instance = pyaudio.PyAudio()
            log.debug("PyAudio instance created (legacy stub).")
        except Exception as e:
            log.warning("PyAudio not available – legacy stub will no‑op: %s", e)
    return _pyaudio_instance


def terminate_pyaudio_instance():
    """Called at shutdown by other modules – safe even if PyAudio isn’t used."""
    global _pyaudio_instance
    if _pyaudio_instance:
        try:
            _pyaudio_instance.terminate()
            log.debug("PyAudio instance terminated (legacy stub).")
        except Exception as e:
            log.warning("Error terminating PyAudio stub: %s", e)
        finally:
            _pyaudio_instance = None


# ------------------------------------------------------------------#
# File helpers                                                       #
# ------------------------------------------------------------------#
def save_stream_to_file(stream, filepath: str) -> None:
    """Save a streaming response (e.g. ElevenLabs) to *filepath*."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "wb") as f:
        for chunk in stream:
            f.write(chunk)
    log.info("Stream saved to %s", filepath)


def convert_mp3_to_wav(mp3_filepath: str, wav_filepath: str) -> None:
    """
    Convert MP3 → WAV.

    We re‑encode at 44 100 Hz, 16‑bit, stereo to keep things simple and to match
    the default format used in your original (‘working’) implementation.
    ALSA’s plug‑ins will transparently resample if the PCM5102 is clocked to
    another rate (48 kHz, 96 kHz, …).
    """
    try:
        audio = AudioSegment.from_mp3(mp3_filepath)
    except CouldntDecodeError as e:
        log.error("ffmpeg could not decode %s: %s", mp3_filepath, e)
        raise
    audio = (
        audio.set_frame_rate(44_100)
        .set_sample_width(2)   # 16‑bit
        .set_channels(2)       # stereo
    )
    os.makedirs(os.path.dirname(wav_filepath), exist_ok=True)
    audio.export(wav_filepath, format="wav")
    log.info("Converted %s → %s (44.1 kHz 16‑bit stereo)", mp3_filepath, wav_filepath)


# ------------------------------------------------------------------#
# Playback                                                           #
# ------------------------------------------------------------------#
def _system_play(cmd: list[str]) -> None:
    """Run *cmd* and raise on failure."""
    try:
        log.info("Playing via system command: %s", " ".join(cmd))
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        log.error("System player not found: %s", cmd[0])
        raise
    except subprocess.CalledProcessError as e:
        log.error("System player returned error: %s", e)
        raise


def play_audio(filepath: str) -> None:
    """
    Play a WAV file.

    • On Linux we call `aplay -D <device> <file>`.
    • On macOS we use `afplay`.
    • On Windows (or anything else) we fall back to `aplay`.

    Using the system player lets ALSA/CoreAudio handle channel‑count and
    sample‑rate conversions; this eliminates the “lento y grave” artefact that
    appeared with the previous manual‑resampling PyAudio path.
    """
    if not os.path.exists(filepath):
        log.error("play_audio: File not found – %s", filepath)
        return
    if not filepath.lower().endswith(".wav"):
        log.error("play_audio: Only WAV files are supported (%s)", filepath)
        return

    if sys.platform.startswith("linux"):
        cmd = ["aplay", "-D", _DAC_DEVICE, filepath]
    elif sys.platform == "darwin":
        cmd = ["afplay", filepath]
    else:  # Windows, *BSD, etc. – assume aplay is in PATH
        cmd = ["aplay", filepath]

    _system_play(cmd)
