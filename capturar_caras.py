"""
capturar_caras.py
------------------
Captura fotos de referencia de una persona con la cámara de la Raspberry
Pi y las guarda en known_faces/<nombre>/foto_XX.jpg

Uso:
    python3 capturar_caras.py

Controles (con la ventana de video en foco):
    c / ESPACIO -> capturar foto
    q / ESC     -> salir

Recomendación: sacá 5-8 fotos por persona, variando un poco el ángulo
de la cara (de frente, girado un poco a cada lado, con y sin luz
directa) para que el reconocimiento sea más robusto.
"""

import os
import cv2
from picamera2 import Picamera2

CARPETA_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "known_faces")


def main():
    nombre = input("Nombre de la persona (sin espacios, ej: Franco): ").strip()
    if not nombre:
        print("Nombre vacío, cancelado.")
        return

    carpeta_persona = os.path.join(CARPETA_BASE, nombre)
    os.makedirs(carpeta_persona, exist_ok=True)

    existentes = [f for f in os.listdir(carpeta_persona) if f.lower().endswith(".jpg")]
    contador = len(existentes)

    picam2 = Picamera2()
    config = picam2.create_preview_configuration(main={"size": (640, 480)})
    picam2.configure(config)
    picam2.start()

    print(f"\nListo. Ventana abierta. Parado/a de frente a la cámara.")
    print("Presioná 'c' o ESPACIO para capturar, 'q' para salir.\n")

    try:
        while True:
            frame = picam2.capture_array()
            frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

            texto = f"{nombre}: {contador} fotos guardadas  |  'c'=capturar  'q'=salir"
            cv2.putText(frame_bgr, texto, (10, 25), cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, (0, 255, 0), 2)
            cv2.imshow("Captura de caras", frame_bgr)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord('c'), ord(' ')):
                contador += 1
                ruta = os.path.join(carpeta_persona, f"foto_{contador:02d}.jpg")
                cv2.imwrite(ruta, frame_bgr)
                print(f"  ✅ Guardada: {ruta}")
            elif key in (ord('q'), 27):
                break
    finally:
        picam2.stop()
        cv2.destroyAllWindows()

    print(f"\nListo. {contador} fotos totales en {carpeta_persona}")
    print("Corré generar_encodings.py cuando termines de capturar a todas las personas.")


if __name__ == "__main__":
    main()
