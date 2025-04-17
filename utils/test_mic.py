#!/usr/bin/env python3
import os
import time
import threading
import pyaudio
import wave
import numpy as np  # pip3 install numpy

# Flag para detener la grabación
stop_flag = False

def wait_for_enter():
    """Espera a que el usuario pulse ENTER y marca stop_flag."""
    global stop_flag
    input()
    stop_flag = True

def record_to_file(pa, device_index, rate, channels, out_path):
    """Graba audio hasta que se pulse ENTER y lo guarda en out_path,
    aplicando normalización para subir el volumen."""
    global stop_flag
    stop_flag = False

    stream = pa.open(format=pyaudio.paInt16,
                     channels=channels,
                     rate=rate,
                     input=True,
                     frames_per_buffer=1024,
                     input_device_index=device_index)

    frames = []
    print(f"\n🔴 Grabando a {rate} Hz, {channels} canal(es)... pulsa ENTER para parar.")

    # Hilo que espera el ENTER
    t = threading.Thread(target=wait_for_enter)
    t.start()

    while not stop_flag:
        try:
            data = stream.read(1024, exception_on_overflow=False)
            frames.append(data)
        except OSError as e:
            # Ignorar overflow y seguir grabando
            print(f"[Warning] overflow: {e}")

    t.join()
    stream.stop_stream()
    stream.close()

    # Combina los frames y convierte a array de int16
    raw = b''.join(frames)
    audio = np.frombuffer(raw, dtype=np.int16)
    if audio.size:
        # Normaliza hasta 90% del rango para subir volumen
        peak = np.max(np.abs(audio))
        gain = int(0.9 * 32767 / peak) if peak > 0 else 1
        if gain > 1:
            audio = np.clip(audio * gain, -32768, 32767).astype(np.int16)
            print(f"🔊 Ganancia aplicada: {gain}×")
    else:
        print("⚠️ Atención: no se detectó audio.")

    # Guarda el WAV
    wf = wave.open(out_path, 'wb')
    wf.setnchannels(channels)
    wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
    wf.setframerate(rate)
    wf.writeframes(audio.tobytes())
    wf.close()
    print(f"✅ Guardado → {out_path}\n")

def main():
    pa = pyaudio.PyAudio()

    # 1) Lista dispositivos de entrada
    print("Dispositivos de entrada disponibles:")
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            print(f"  [{i}] {info['name']}")

    # 2) Elige dispositivo (el mic USB)
    dev = int(input("\nIntroduce el ID de tu micrófono USB: "))

    # 3) Usa parámetros por defecto del dispositivo
    info = pa.get_device_info_by_index(dev)
    rate = int(info["defaultSampleRate"])
    channels = int(info["maxInputChannels"])
    print(f"\nUsando {rate} Hz y {channels} canal(es) por defecto para ese dispositivo.")

    # 4) Prepara carpeta de salida
    out_dir = "recordings"
    os.makedirs(out_dir, exist_ok=True)
    print(f"\nLos WAV se guardarán en ./{out_dir}/")

    # 5) Bucle: ENTER para grabar, 'q' para salir
    print("\nPulsa ENTER para grabar, o escribe 'q' + ENTER para salir.")
    while True:
        cmd = input(">> ")
        if cmd.lower() == 'q':
            break
        if cmd == '':
            ts = time.strftime("%Y%m%d-%H%M%S")
            fname = f"rec_{ts}.wav"
            path = os.path.join(out_dir, fname)
            record_to_file(pa, dev, rate, channels, path)

    pa.terminate()
    print("👋 ¡Adiós!")

if __name__ == "__main__":
    main()
