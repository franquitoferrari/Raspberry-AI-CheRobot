# Reconocimiento facial → acciones del robot

## Qué hace cada archivo

| Archivo | Para qué sirve |
|---|---|
| `Robot.py` | Tu app original + un agregado: cada 500 ms revisa si apareció `gatillo_facial.txt` y, si aparece, reproduce `secuencias/<nombre>.json` (mismo motor que ya usa la voz). Reemplaza tu `Robot.py` actual por este. |
| `capturar_caras.py` | Saca fotos tuyas y de tus amigos con la cámara, para armar la base de caras conocidas. |
| `generar_encodings.py` | Convierte esas fotos en `encodings.pkl` (los "vectores" matemáticos de cada cara). |
| `reconocimiento_facial.py` | Mira la cámara en vivo, reconoce caras y escribe en `gatillo_facial.txt` cuando detecta a alguien conocido. |

## Instalación (una sola vez)

```bash
sudo apt update
sudo apt install -y cmake python3-opencv
pip3 install face_recognition --break-system-packages
```

⚠️ `face_recognition` depende de `dlib`, que se compila desde cero en la Raspberry — puede tardar **20-40 minutos** en instalar. Es normal, dejalo correr.

## Paso 1: armar la base de caras

```bash
python3 capturar_caras.py
```
- Te pide un nombre (ej: `Franco`), abre la cámara.
- Apretás `c` para cada foto (sacá 5-8, variando ángulo/luz).
- `q` para salir.
- Repetí el proceso una vez por cada persona (corré el script de nuevo con el otro nombre).

Esto arma:
```
known_faces/
  Franco/
    foto_01.jpg
    foto_02.jpg
    ...
  Amigo1/
    foto_01.jpg
    ...
```

## Paso 2: generar los encodings

```bash
python3 generar_encodings.py
```
Genera `encodings.pkl`. **Corré esto de nuevo cada vez que agregues fotos nuevas.**

## Paso 3: configurar las acciones

Editá `reconocimiento_facial.py` y ajustá:

1. **`RUTA_PROYECTO`**: la carpeta desde donde corrés `Robot.py` (según tu historial, algo como `/home/mecabot/Desktop`). Tiene que ser la misma carpeta siempre, para que ambos scripts se "encuentren" a través de `gatillo_facial.txt`.

2. **`PERSONA_A_ACCION`**: el diccionario nombre → secuencia. Por ejemplo:
   ```python
   PERSONA_A_ACCION = {
       "Franco": "saludo",
       "Nico": "pelea",
   }
   ```
   El valor (`"saludo"`, `"pelea"`) tiene que coincidir **exacto** con el nombre de un archivo dentro de `secuencias/`, sin `.json` (o sea, tiene que existir `secuencias/pelea.json`). Si tu secuencia de pelea/boxeo ya existe con otro nombre (por ejemplo `boxeo.json`, mencionado en tu informe como macro `#boxeo`), usá ese nombre en vez de `"pelea"`.

## Paso 4: correr todo junto

Necesitás **dos terminales** (o dos pestañas SSH) abiertas al mismo tiempo, desde la carpeta del proyecto:

**Terminal 1** — el robot (igual que siempre):
```bash
cd ~/Desktop
python3 Robot.py
```

**Terminal 2** — el reconocimiento facial:
```bash
cd ~/Desktop
python3 reconocimiento_facial.py
```

Cuando la cámara reconozca a Franco, va a escribir `saludo` en `gatillo_facial.txt`. `Robot.py` lo va a leer en menos de medio segundo y va a reproducir `secuencias/saludo.json` por el puerto serie, tal cual lo hace hoy con la voz.

## Ajustes que probablemente quieras tocar

- **`COOLDOWN_SEGUNDOS`** (en `reconocimiento_facial.py`): cuánto esperar antes de repetir la misma acción con la misma persona parada en cámara. Por defecto 20 segundos.
- **`TOLERANCIA`**: qué tan estricta es la comparación de caras (0.55 por defecto). Si te confunde con otra persona, bajala a 0.5. Si no te reconoce bien, subila a 0.6.
- **`PROCESAR_CADA_N_FRAMES`**: la Raspberry no da abasto para analizar cada frame de video en tiempo real. Si ves que va muy lento o se traba, subí este número (analiza menos seguido).
- **`MOSTRAR_VENTANA`**: ponelo en `False` si vas a correr esto por SSH sin monitor conectado a la Raspberry.

## Nota de rendimiento

El reconocimiento facial con `face_recognition` corre 100% en la CPU de la Raspberry (el chip IA de la cámara IMX500 no sirve para esto, solo para los modelos `.rpk` que ya veníamos usando). Con el modelo `"hog"` (el que uso en el script) es razonable en una Pi 4, pero no esperes 30 fps — es normal que la detección tarde uno o dos segundos en "engachar" a la persona.
