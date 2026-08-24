"""
reconocimiento_facial.py
-------------------------
Mira por la cámara de la Raspberry, reconoce caras conocidas (generadas
con capturar_caras.py + generar_encodings.py) y avisa a Robot.py qué
secuencia reproducir, escribiendo el nombre en gatillo_facial.txt.

Robot.py debe estar corriendo al mismo tiempo (con el parche que agrega
_check_trigger_facial) y conectado al puerto serie del robot, porque es
quien realmente envía los comandos a los servos.

IMPORTANTE: ajustá las constantes de la sección CONFIGURACIÓN antes de
correrlo.

Uso:
    python3 reconocimiento_facial.py
"""

import os
import time
import pickle
import cv2
import face_recognition
from picamera2 import Picamera2

# ======================= CONFIGURACIÓN =======================
# Carpeta desde la que corrés Robot.py (la misma donde Robot.py crea
# "secuencias/" y desde donde este script debe escribir el gatillo).
# Ajustá esto a tu caso real, por ejemplo "/home/mecabot/Desktop".
RUTA_PROYECTO = "/home/mecabot/Desktop"

# Qué secuencia (nombre del .json en secuencias/, SIN ".json") disparar
# según qué persona reconoce. Editá con tus nombres reales.
# El nombre debe coincidir EXACTO con la carpeta que usaste en
# capturar_caras.py, y el valor debe coincidir EXACTO con el nombre del
# archivo .json dentro de secuencias/ (ej: secuencias/pelea.json).
PERSONA_A_ACCION = {
    "Franco": "saludo",
    "Amigo1": "pelea",   # <-- cambiá "Amigo1" por el nombre que usaste
}

# Cuánto esperar (segundos) antes de volver a disparar la MISMA acción,
# para que no repita el saludo/pelea en bucle mientras la persona sigue
# frente a la cámara.
COOLDOWN_SEGUNDOS = 20

# Qué tan estricta es la comparación de caras. Más bajo = más estricto
# (menos falsos positivos, pero puede no reconocerte en mala luz).
# 0.6 es el default de la librería; 0.5 es más estricto.
TOLERANCIA = 0.55

# Procesar 1 de cada N frames (la Raspberry no da abasto para analizar
# cada frame a resolución completa). Subilo si ves que va muy lento.
PROCESAR_CADA_N_FRAMES = 5

# Mostrar ventana con el video y los nombres detectados (poné False si
# corrés esto sin monitor conectado, por SSH).
MOSTRAR_VENTANA = True
# ===============================================================

ARCHIVO_ENCODINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "encodings.pkl")
TRIGGER_FILE = os.path.join(RUTA_PROYECTO, "gatillo_facial.txt")


def cargar_encodings():
    if not os.path.exists(ARCHIVO_ENCODINGS):
        raise FileNotFoundError(
            f"No encontré {ARCHIVO_ENCODINGS}. Corré primero capturar_caras.py y generar_encodings.py"
        )
    with open(ARCHIVO_ENCODINGS, "rb") as f:
        return pickle.load(f)


def disparar_accion(nombre_accion):
    """Escribe el nombre de la secuencia en el archivo que Robot.py está pollendo."""
    try:
        with open(TRIGGER_FILE, "w", encoding="utf-8") as f:
            f.write(nombre_accion)
        print(f"  🤖 Gatillo enviado: '{nombre_accion}'")
    except Exception as e:
        print(f"  ⚠️  No pude escribir el gatillo en {TRIGGER_FILE}: {e}")


def main():
    encodings_conocidos = cargar_encodings()

    # Aplanar a dos listas paralelas para comparar más fácil
    nombres_flat = []
    encodings_flat = []
    for nombre, lista in encodings_conocidos.items():
        for enc in lista:
            nombres_flat.append(nombre)
            encodings_flat.append(enc)

    print(f"Personas conocidas: {sorted(set(nombres_flat))}")
    print(f"Gatillo se escribe en: {TRIGGER_FILE}")

    ultimo_disparo = {}  # nombre_accion -> timestamp del último disparo

    picam2 = Picamera2()
    config = picam2.create_preview_configuration(main={"size": (640, 480)})
    picam2.configure(config)
    picam2.start()

    frame_num = 0

    print("\nArrancó el reconocimiento. Ctrl+C para salir.\n")

    try:
        while True:
            frame_rgb = picam2.capture_array()  # picamera2 entrega RGB
            frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)  # para dibujar/mostrar con OpenCV
            frame_num += 1

            if frame_num % PROCESAR_CADA_N_FRAMES == 0:
                # Reducimos la imagen para que el análisis sea más rápido
                pequena_rgb = cv2.resize(frame_rgb, (0, 0), fx=0.5, fy=0.5)

                ubicaciones = face_recognition.face_locations(pequena_rgb, model="hog")
                caras = face_recognition.face_encodings(pequena_rgb, ubicaciones)

                for (top, right, bottom, left), encoding in zip(ubicaciones, caras):
                    nombre_detectado = "Desconocido"

                    if encodings_flat:
                        distancias = face_recognition.face_distance(encodings_flat, encoding)
                        mejor_idx = distancias.argmin()
                        if distancias[mejor_idx] <= TOLERANCIA:
                            nombre_detectado = nombres_flat[mejor_idx]

                    # Escalar coordenadas de vuelta al tamaño original (para dibujar)
                    top, right, bottom, left = top * 2, right * 2, bottom * 2, left * 2

                    if MOSTRAR_VENTANA:
                        color = (0, 255, 0) if nombre_detectado != "Desconocido" else (0, 0, 255)
                        cv2.rectangle(frame_bgr, (left, top), (right, bottom), color, 2)
                        cv2.putText(frame_bgr, nombre_detectado, (left, top - 10),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

                    if nombre_detectado in PERSONA_A_ACCION:
                        accion = PERSONA_A_ACCION[nombre_detectado]
                        ahora = time.time()
                        if ahora - ultimo_disparo.get(accion, 0) >= COOLDOWN_SEGUNDOS:
                            print(f"👀 Reconocido: {nombre_detectado} -> acción '{accion}'")
                            disparar_accion(accion)
                            ultimo_disparo[accion] = ahora

            if MOSTRAR_VENTANA:
                cv2.imshow("Reconocimiento facial", frame_bgr)
                if (cv2.waitKey(1) & 0xFF) in (ord('q'), 27):
                    break

    except KeyboardInterrupt:
        pass
    finally:
        picam2.stop()
        if MOSTRAR_VENTANA:
            cv2.destroyAllWindows()
        print("\nCortado.")


if __name__ == "__main__":
    main()
