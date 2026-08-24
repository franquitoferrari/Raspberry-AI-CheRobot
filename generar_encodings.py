"""
generar_encodings.py
---------------------
Recorre known_faces/<nombre>/*.jpg, calcula el "encoding" facial
(vector de 128 valores) de cada foto con la librería face_recognition,
y guarda todo en encodings.pkl para que reconocimiento_facial.py lo use.

Correr esto cada vez que agregues o saques fotos de known_faces/.

Uso:
    python3 generar_encodings.py
"""

import os
import pickle
import face_recognition

CARPETA_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "known_faces")
ARCHIVO_SALIDA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "encodings.pkl")


def main():
    if not os.path.isdir(CARPETA_BASE):
        print(f"No existe la carpeta {CARPETA_BASE}. Corré primero capturar_caras.py")
        return

    encodings_por_nombre = {}

    personas = sorted(
        d for d in os.listdir(CARPETA_BASE)
        if os.path.isdir(os.path.join(CARPETA_BASE, d))
    )

    if not personas:
        print("No hay ninguna carpeta de persona dentro de known_faces/.")
        return

    for nombre in personas:
        carpeta_persona = os.path.join(CARPETA_BASE, nombre)
        fotos = [f for f in os.listdir(carpeta_persona) if f.lower().endswith((".jpg", ".jpeg", ".png"))]

        if not fotos:
            print(f"⚠️  {nombre}: no tiene fotos, se salta.")
            continue

        lista_encodings = []
        for foto in fotos:
            ruta = os.path.join(carpeta_persona, foto)
            imagen = face_recognition.load_image_file(ruta)
            caras = face_recognition.face_encodings(imagen)
            if len(caras) == 0:
                print(f"  ⚠️  No se detectó ninguna cara en {foto}, se ignora.")
                continue
            if len(caras) > 1:
                print(f"  ⚠️  Se detectó más de una cara en {foto}, se usa la primera.")
            lista_encodings.append(caras[0])

        if lista_encodings:
            encodings_por_nombre[nombre] = lista_encodings
            print(f"✅ {nombre}: {len(lista_encodings)} encoding(s) generado(s) de {len(fotos)} foto(s).")
        else:
            print(f"⚠️  {nombre}: ninguna foto tenía una cara detectable.")

    if not encodings_por_nombre:
        print("\nNo se generó ningún encoding. Revisá las fotos en known_faces/.")
        return

    with open(ARCHIVO_SALIDA, "wb") as f:
        pickle.dump(encodings_por_nombre, f)

    print(f"\n💾 Guardado: {ARCHIVO_SALIDA}")
    print(f"Personas registradas: {', '.join(encodings_por_nombre.keys())}")


if __name__ == "__main__":
    main()
