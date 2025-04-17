#!/usr/bin/env python3
import os
import time

import sounddevice as sd
import soundfile as sf

def list_input_devices():
    print("Available audio input devices:")
    devices = sd.query_devices()
    inputs = [(i, d) for i, d in enumerate(devices) if d['max_input_channels'] > 0]
    for idx, d in inputs:
        print(f"  {idx}: {d['name']}  (max channels: {d['max_input_channels']})")
    return [idx for idx, _ in inputs]

def record_to_file(device, samplerate, channels, duration, out_path):
    print(f"\nRecording {duration}s from device {device}...")
    sd.default.device = device
    sd.default.samplerate = samplerate
    sd.default.channels = channels

    data = sd.rec(int(duration * samplerate))
    sd.wait()
    sf.write(out_path, data, samplerate)
    print(f"Saved → {out_path}\n")

def main():
    # 1) show devices
    valid_ids = list_input_devices()

    # 2) choose device
    while True:
        try:
            dev = int(input("\nEnter the device ID of your USB mic: "))
            if dev in valid_ids:
                break
            else:
                print("Invalid ID, please choose one from the list above.")
        except ValueError:
            print("Please enter a number.")

    # 3) parameters
    fs = input("Sample rate in Hz [default 44100]: ").strip() or "44100"
    ch = input("Number of channels [default 1]: ").strip() or "1"
    try:
        fs = int(fs)
        ch = int(ch)
    except ValueError:
        print("Invalid input—using defaults 44100Hz, 1 channel.")
        fs, ch = 44100, 1

    # 4) ensure output folder
    out_dir = "mic_recordings"
    os.makedirs(out_dir, exist_ok=True)
    print(f"\nRecordings will be saved in ./{out_dir}/")

    # 5) loop: record on demand
    print("\nPress ENTER to start a new recording, or type 'q' + ENTER to quit.")
    while True:
        cmd = input(">> ")
        if cmd.lower() == 'q':
            print("Exiting. Goodbye!")
            break

        try:
            dur = float(input("Duration (seconds): "))
        except ValueError:
            print("Invalid duration. Try again.")
            continue

        timestamp = time.strftime("%Y%m%d-%H%M%S")
        filename = f"recording_{timestamp}.wav"
        out_path = os.path.join(out_dir, filename)
        record_to_file(dev, fs, ch, dur, out_path)

if __name__ == "__main__":
    main()
