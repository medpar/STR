#!/usr/bin/env python3
import os, time
import pyaudio
import wave

def list_input_devices(pa):
    print("Available audio input devices:")
    info = pa.get_host_api_info_by_index(0)
    for i in range(info["deviceCount"]):
        dev = pa.get_device_info_by_host_api_device_index(0, i)
        if dev["maxInputChannels"] > 0:
            print(f"  {i}: {dev['name']} (channels: {dev['maxInputChannels']})")

def record_to_file(pa, device_index, rate, channels, duration, out_path):
    print(f"\nRecording {duration}s → {out_path}")
    stream = pa.open(format=pyaudio.paInt16,
                     channels=channels,
                     rate=rate,
                     input=True,
                     frames_per_buffer=1024,
                     input_device_index=device_index)

    frames = []
    for _ in range(int(rate / 1024 * duration)):
        frames.append(stream.read(1024))
    stream.stop_stream()
    stream.close()

    wf = wave.open(out_path, 'wb')
    wf.setnchannels(channels)
    wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
    wf.setframerate(rate)
    wf.writeframes(b''.join(frames))
    wf.close()
    print("Done.\n")

def main():
    pa = pyaudio.PyAudio()
    list_input_devices(pa)

    dev = int(input("\nEnter your mic’s device ID: "))
    rate = int(input("Sample rate [44100]: ") or 44100)
    ch   = int(input("Channels [1]: ")    or 1)

    os.makedirs("recordings", exist_ok=True)
    print("\nPress ENTER to record, ‘q’ to quit.")
    while True:
        if input(">> ").lower() == 'q':
            break
        dur = float(input("Duration (s): "))
        ts  = time.strftime("%Y%m%d-%H%M%S")
        path= f"recordings/rec_{ts}.wav"
        record_to_file(pa, dev, rate, ch, dur, path)

    pa.terminate()

if __name__=="__main__":
    main()
