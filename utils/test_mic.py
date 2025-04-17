#!/usr/bin/env python3
import os
import time
import threading
import subprocess

# Flag para detener la grabación
stop_flag = False

def wait_for_enter():
    """Espera a que el usuario pulse ENTER y marca stop_flag."""
    global stop_flag
    input()
    stop_flag = True

def record_to_file(device, out_path):
    """Lanza arecord hasta que se pulse ENTER y guarda en out_path."""
    global stop_flag
    stop_flag = False

    # Comando arecord: dispositivo, formato CD (44.1kHz, 16bit, stereo), WAV
    cmd = [
        "arecord",
        "-D", device,
        "-f", "cd",
        "-t", "wav",
        out_path
    ]
    print(f"\n🔴 Grabando con arecord en {device}… pulsa ENTER para parar.")
    proc = subprocess.Popen(cmd)

    # Hilo que espera ENTER
    t = threading.Thread(target=wait_for_enter)
    t.start()

    # Mantén el proceso hasta que stop_flag sea True
    while not stop_flag:
        time.sleep(0.1)

    # Detén arecord
    proc.terminate()
    proc.wait()
    t.join()

    print(f"✅ Guardado → {out_path}\n")

def main():
    # 1) Dispositivo ALSA (por defecto plughw:1,0)
    device = input("Dispositivo ALSA [plughw:1,0]: ").strip() or "plughw:1,0"

    # 2) Carpeta de salida
    out_dir = "recordings"
    os.makedirs(out_dir, exist_ok=True)
    print(f"\nLos WAV se guardarán en ./{out_dir}/")

    # 3) Bucle principal: ENTER para grabar, 'q' para salir
    print("\nPulsa ENTER para grabar, o escribe 'q' + ENTER para salir.")
    while True:
        cmd = input(">> ")
        if cmd.lower() == 'q':
            break
        if cmd == '':
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            filename = f"rec_{timestamp}.wav"
            path = os.path.join(out_dir, filename)
            record_to_file(device, path)

    print("👋 ¡Adiós!")

if __name__ == "__main__":
    main()
