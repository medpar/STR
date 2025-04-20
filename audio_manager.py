#!/usr/bin/env python3
"""
Save, convert and play audio – now resampling everything to OUTPUT_SAMPLE_RATE.
"""

import os
import logging
from pydub import AudioSegment
import pyaudio

from config import DAC_PYAUDIO_INDEX, PLAYBACK_CHUNK, OUTPUT_SAMPLE_RATE

log = logging.getLogger(__name__)


def save_stream_to_file(stream, filepath):
    """Save streaming data to a file."""
    with open(filepath, "wb") as f:
        for chunk in stream:
            f.write(chunk)
    log.info("File saved: %s", filepath)


def convert_mp3_to_wav(mp3_filepath, wav_filepath):
    """Convert MP3 → WAV, then resample to OUTPUT_SAMPLE_RATE stereo 16‑bit."""
    audio = AudioSegment.from_mp3(mp3_filepath)
    audio = audio.set_frame_rate(OUTPUT_SAMPLE_RATE).set_sample_width(2).set_channels(2)
    audio.export(wav_filepath, format="wav")
    log.info("Converted %s → %s (%d Hz, 16‑bit, Stereo)", mp3_filepath, wav_filepath, OUTPUT_SAMPLE_RATE)


def play_audio(filepath):
    """Play any WAV audio file, resampled to OUTPUT_SAMPLE_RATE."""
    if not os.path.exists(filepath):
        log.error("Playback error: File not found - %s", filepath)
        return

    # Load & resample
    audio = AudioSegment.from_wav(filepath)
    audio = audio.set_frame_rate(OUTPUT_SAMPLE_RATE)
    raw_data = audio.raw_data
    channels = audio.channels
    sample_width = audio.sample_width
    rate = OUTPUT_SAMPLE_RATE

    p = pyaudio.PyAudio()
    fmt = p.get_format_from_width(sample_width)
    log.info("Playing: %s (%d Hz, %d channels, %d‑byte samples) on device %d",
             filepath, rate, channels, sample_width, DAC_PYAUDIO_INDEX)

    stream = p.open(format=fmt,
                    channels=channels,
                    rate=rate,
                    output=True,
                    output_device_index=DAC_PYAUDIO_INDEX,
                    frames_per_buffer=PLAYBACK_CHUNK)

    # Write in chunks
    byte_per_frame = sample_width * channels
    chunk_bytes = PLAYBACK_CHUNK * byte_per_frame
    for start in range(0, len(raw_data), chunk_bytes):
        stream.write(raw_data[start:start + chunk_bytes])

    stream.stop_stream()
    stream.close()
    p.terminate()
    log.info("Finished playing: %s", filepath)
