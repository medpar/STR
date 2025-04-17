#!/usr/bin/env python3
"""
Standalone tester for USB‑mic recording + GPIO button / LED.

• Press the push‑button  -> LED ON, recording starts
• Press again            -> LED OFF, recording stops & WAV is saved
• Ctrl‑C exits

❗ Wiring (BCM numbering, same as realtime.py):

Push‑button : GPIO17  <‑‑>  GND   (internal pull‑up enabled)
LED anode   : GPIO27 through 330 Ω resistor  →  LED  →  GND

If your button is flaky add an external 10 kΩ pull‑up to 3 V3.
"""

import os, time, wave, threading, sys
from pathlib import Path
import numpy as np
import pyaudio

# ---- import parent‑level config.py ---- #
sys.path.append(str(Path(__file__).resolve().parents[1]))
from config import (            # type: ignore
    MIC_DEVICE_INDEX,
    SAMPLE_RATE,
    NUM_CHANNELS,
    FRAME_CHUNK,
    NORMALISE_INPUT,
    GPIO_BUTTON_PIN,
    GPIO_LED_PIN,
)

try:
    from gpiozero import Button, LED        # type: ignore
except (ImportError, RuntimeError):
    print("⚠️  gpiozero not available – run on Raspberry Pi.")
    Button = LED = None                     # type: ignore


# ------------------------------------------------------------#
#  Audio helpers                                              #
# ------------------------------------------------------------#
pa = pyaudio.PyAudio()
stream_in = None
frames: list[bytes] = []
recording = False
lock = threading.Lock()


def start_stream():
    global stream_in, frames, recording
    stream_in = pa.open(
        format=pyaudio.paInt16,
        channels=NUM_CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=FRAME_CHUNK,
        input_device_index=MIC_DEVICE_INDEX,
    )
    frames = []
    recording = True
    print("🎙️  REC  (press button again to stop)")


def stop_stream():
    global stream_in, recording
    if not stream_in:
        return
    stream_in.stop_stream()
    stream_in.close()
    stream_in = None
    recording = False
    print("⏹️  STOP")


def audio_thread():
    """Read chunks in background while `recording` flag is True."""
    global frames
    while True:
        if recording and stream_in:
            data = stream_in.read(FRAME_CHUNK, exception_on_overflow=False)
            if NORMALISE_INPUT:
                # chunk‑wise normalisation
                audio = np.frombuffer(data, np.int16)
                peak = np.max(np.abs(audio)) or 1
                gain = int(0.9 * 32767 / peak)
                if gain > 1:
                    audio = np.clip(audio * gain, -32768, 32767).astype(np.int16)
                    data = audio.tobytes()
            frames.append(data)
        else:
            time.sleep(0.01)


# ------------------------------------------------------------#
#  GPIO callback                                              #
# ------------------------------------------------------------#
def button_pressed():
    with lock:
        if recording:
            led.off()
            stop_stream()
            save_wav()
        else:
            start_stream()
            led.on()


def save_wav():
    if not frames:
        print("⚠️  No audio captured.")
        return
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path("recordings")
    out_dir.mkdir(exist_ok=True)
    fname = out_dir / f"test_{ts}.wav"
    wf = wave.open(str(fname), "wb")
    wf.setnchannels(NUM_CHANNELS)
    wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
    wf.setframerate(SAMPLE_RATE)
    wf.writeframes(b"".join(frames))
    wf.close()
    print(f"✅ WAV saved → {fname.resolve()}")


# ------------------------------------------------------------#
#  Main                                                       #
# ------------------------------------------------------------#
if __name__ == "__main__":
    print(
        f"📌 Mic index={MIC_DEVICE_INDEX}, rate={SAMPLE_RATE} Hz, "
        f"GPIO button={GPIO_BUTTON_PIN}, LED={GPIO_LED_PIN}"
    )

    # GPIO setup
    if Button is None:
        print("Run on Pi for GPIO test. Exiting.")
        sys.exit(1)

    button = Button(GPIO_BUTTON_PIN, pull_up=True, bounce_time=0.1)
    led = LED(GPIO_LED_PIN)
    button.when_pressed = button_pressed

    # background audio capture
    threading.Thread(target=audio_thread, daemon=True).start()

    try:
        print("Press the push‑button to start/stop recording. Ctrl‑C to exit.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        led.off()
        if recording:
            stop_stream()
            save_wav()
        pa.terminate()
        print("👋 Bye")
