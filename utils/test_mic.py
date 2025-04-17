#!/usr/bin/env python3
import os
import time
import threading
import pyaudio
import wave

# Flag para detener la grabación
stop_flag = False

def wait_for_enter():
    """Espera a que el usuario pulse ENTER y marca stop_flag."""
    global stop_flag
    input()  # bloquea hasta ENTER
    stop_flag = True

def record_to_file(pa, device_index, rate, channels, out_path):
    """Graba audio hasta que se pulse ENTER de nuevo y lo guarda en out_path."""
    global stop_flag
    stop_flag = False

    stream = pa.open(format=pyaudio.paInt16,
                     channels=channels,
                     rate=rate,
                     input=True,
                     frames_per_buffer=1024,
                     input_device_index=device_index)

    frames = []
    print("\n🔴 Grabando... pulsa ENTER para parar.")

    # Arranca hilo que espera el ENTER
    t = threading.Thread(target=wait_for_enter)
    t.start()

    # Lee fragmentos hasta que stop_flag sea True
    while not stop_flag:
        try:
            data = stream.read(1024, exception_on_overflow=False)
            frames.append(data)
        except OSError as e:
            # Si ocurre overflow, lo ignoramos y seguimos
            print(f"[Warning] overflow: {e}")

    t.join()
    stream.stop_stream()
    stream.close()

    # Escribe el fichero WAV
    wf = wave.open(out_path, 'wb')
    wf.setnchannels(channels)
    wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
    wf.setframerate(rate)
    wf.writeframes(b''.join(frames))
    wf.close()
    print(f"✅ Guardado → {out_path}\n")

def main():
    pa = pyaudio.PyAudio()

    # 1) Listar dispositivos de entrada
    print("Dispositivos de entrada disponibles:")
    for i in range(pa.get_device_count()):
        dev = pa.get_device_info_by_index(i)
        if dev["maxInputChannels"] > 0:
            print(f"  [{i}] {dev['name']}  (canales: {dev['maxInputChannels']})")

    # 2) Elegir dispositivo, sample rate y canales
    dev = int(input("\nIntroduce el ID de tu micrófono USB: "))
    rate = int(input("Sample rate (Hz) [44100]: ") or 44100)
    chans = int(input("Canales [1]: ") or 1)

    # 3) Carpeta de salida
    out_dir = "recordings"
    os.makedirs(out_dir, exist_ok=True)
    print(f"\nLos WAV se guardarán en ./{out_dir}/")

    # 4) Bucle principal: ENTER para grabar, 'q' para salir
    print("\nPulsa ENTER para grabar, o escribe 'q' + ENTER para salir.")
    while True:
        cmd = input(">> ")
        if cmd.lower() == 'q':
            break
        if cmd == '':
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            filename = f"rec_{timestamp}.wav"
            path = os.path.join(out_dir, filename)
            record_to_file(pa, dev, rate, chans, path)

    pa.terminate()
    print("👋 ¡Adiós!")

if __name__ == "__main__":
    main()
