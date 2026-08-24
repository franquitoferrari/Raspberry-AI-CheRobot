import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
import serial, serial.tools.list_ports
import time, threading, re, sys, traceback, json, os
import copy  # para copiar pasos

# ======================= CONFIGURACIÓN GENERAL =======================
PWM_MIN = 500
PWM_MAX = 2500
NUM_CANALES = 32
BAUDRATE = 9600
READ_TIMEOUT = 1.0
ACK_TIMEOUT = 60.0
RESP_TIMEOUT = 12.0

# ======================= ESTADO GLOBAL =======================
puerto_serial = None
serial_lock = threading.Lock()
sliders_info = []
updating = False
lbl_fb = None
lbl_lr = None

# Ventana de posiciones
pos_win = None
pos_tree = None
toggle_pos_btn = None  # botón Mostrar/Ocultar

# ASCII / terminal
ascii_send_text = None
rx_text = None
rx_vsb = None
ascii_frame = None
ascii_toggle_btn = None
ascii_visible = True  # estado de visibilidad

# RX live (nuevo toggle)
rx_frame = None
rx_toggle_btn = None
rx_visible = True  # estado de visibilidad

# >>> MONITOR EN VIVO: estado e hilos
_monitor_thread = None
_monitor_stop_evt = threading.Event()
_monitor_pause_evt = threading.Event()
_root_ref = None

# ===== Secuencias: estado =====
secuencia = []  # lista de pasos
seq_tree = None
seq_wait_entry = None
seq_sync_var = None
playback_thread = None
playback_stop_evt = threading.Event()
_seq_clip = None  # portapapeles: lista de pasos copiados (1..N)

# --- Resaltado de paso en ejecución ---
_seq_running_iid = None  # <<< NUEVO: iid (ej. "s12") actualmente resaltado

# ======================= CONVERSIONES =======================
def angulo_a_pwm(angulo):
    return int(round(PWM_MIN + (angulo / 225) * (PWM_MAX - PWM_MIN)))

def pwm_a_angulo(pwm):
    return int(round((pwm - PWM_MIN) * 225 / (PWM_MAX - PWM_MIN)))

# ======================= SERIAL =======================
def puertos_disponibles():
    return [p.device for p in serial.tools.list_ports.comports()]

def _serial_ok():
    return (puerto_serial is not None) and puerto_serial.is_open

def conectar_serial():
    global puerto_serial
    port = combo_puertos.get()
    if not port:
        label_estado.config(text="Elegí un puerto", fg="red")
        return
    try:
        ps = serial.Serial(port, BAUDRATE, timeout=READ_TIMEOUT)
        with serial_lock:
            if puerto_serial and puerto_serial.is_open:
                try:
                    puerto_serial.close()
                except:
                    pass
            puerto_serial = ps
            puerto_serial.reset_input_buffer()
            puerto_serial.reset_output_buffer()
        label_estado.config(text=f"Conectado a {port}", fg="green")
        _monitor_restart()  # reiniciar monitor
    except Exception as e:
        label_estado.config(text=f"Error: {e}", fg="red")

def refrescar_puertos():
    combo_puertos["values"] = puertos_disponibles()

# ======================= UTILIDADES DE LECTURA =======================
def _read_until_predicate(timeout_s, predicate):
    t0 = time.time()
    buf = b''
    while time.time() - t0 <= timeout_s:
        with serial_lock:
            b = puerto_serial.read(1) if _serial_ok() else b''
        if b:
            buf += b
            try:
                if predicate(buf):
                    break
            except Exception:
                pass
        else:
            time.sleep(0.001)
    return buf

def _wait_by_stars_and_regex(min_stars, regex, hard_timeout):
    def _pred(b):
        if b.count(b'*') < min_stars:
            return False
        s = b.decode('ascii', errors='ignore')
        return bool(regex.search(s))
    buf = _read_until_predicate(hard_timeout, _pred)
    return buf.decode(errors='ignore')

def _send_and_read_until(min_stars, regex_pat, hard_timeout, cmd):
    if not _serial_ok():
        raise RuntimeError("Puerto no conectado")
    _monitor_pause(True)  # pausar monitor para no “comer” la respuesta
    try:
        with serial_lock:
            puerto_serial.reset_input_buffer()
            puerto_serial.write(cmd)
        rx = re.compile(regex_pat)
        return _wait_by_stars_and_regex(min_stars, rx, hard_timeout)
    finally:
        _monitor_pause(False)

def _wait_for_ack_star(timeout_s=ACK_TIMEOUT):
    """Espera un '*' del firmware. Pausa el monitor para no consumirlo y lo muestra en RX."""
    if not _serial_ok():
        return False
    _monitor_pause(True)
    try:
        t0 = time.time()
        while time.time() - t0 <= timeout_s:
            with serial_lock:
                n = puerto_serial.in_waiting if hasattr(puerto_serial, 'in_waiting') else 0
                data = puerto_serial.read(n if n and n > 0 else 1)
            if data:
                if b'*' in data:
                    try:
                        if _root_ref:
                            _root_ref.after(0, _rx_append, '*')
                    except:
                        pass
                    return True
            else:
                time.sleep(0.003)
        return False
    finally:
        _monitor_pause(False)

# ======================= ENVÍOS (PWM/Velocidad) =======================
def enviar_paquete(canal, pwm, velocidad):
    if not _serial_ok():
        label_estado.config(text="Puerto no conectado", fg="red")
        return
    pwm = max(PWM_MIN, min(PWM_MAX, int(round(float(pwm)))))
    try:
        vel = max(1, min(15, int(velocidad)))
    except:
        vel = 1
    paquete = f"*{canal:02d}{pwm:04d}{vel:02d}*".encode('ascii')
    with serial_lock:
        puerto_serial.write(paquete)

def enviar_paquete_multiple(items):
    """
    items: lista de tuplas (canal:int, pwm:int, vel:int)
    """
    if not _serial_ok():
        label_estado.config(text="Puerto no conectado", fg="red")
        return False
    if not items:
        return True
    s = "*"
    for ch, pwm, vel in items:
        pwm = max(PWM_MIN, min(PWM_MAX, int(pwm)))
        vel = max(1, min(15, int(vel)))
        s += f"{ch:02d}{pwm:04d}{vel:02d}"
    s += "*"
    with serial_lock:
        puerto_serial.reset_input_buffer()  # limpiar antes de enviar (para que el ACK sea del paso actual)
        puerto_serial.write(s.encode('ascii'))
    return True

def enviar_todos():
    if not _serial_ok():
        messagebox.showwarning("Serial", "Puerto no conectado.")
        return
    paquete = "*"
    alguno = False
    for canal, pwm_slider, vel_var, activo_var, *_ in sliders_info:
        if activo_var.get():
            pwm = int(round(pwm_slider.get()))
            # >>> cambio: clamp de velocidad 01..15
            try:
                vel = max(1, min(15, int(vel_var.get())))
            except:
                vel = 1
            # <<<
            paquete += f"{canal:02d}{pwm:04d}{vel:02d}"
            alguno = True
    paquete += "*"
    if not alguno:
        messagebox.showinfo("Enviar Todos", "No hay canales activos para enviar.")
        return
    with serial_lock:
        puerto_serial.reset_input_buffer()
        puerto_serial.write(paquete.encode('ascii'))
    label_estado.config(text="Paquete múltiple enviado", fg="blue")


def actualizar_desde_pwm(canal, pwm_slider, vel_var, activo_var, pwm_box, vel_box):
    global updating
    if updating:
        return
    updating = True
    try:
        pwm = int(float(pwm_slider.get()))
        pwm_box.config(state='normal'); pwm_box.delete(0, tk.END); pwm_box.insert(0, str(pwm)); pwm_box.config(state='readonly')
    finally:
        updating = False
    if activo_var.get():
        enviar_paquete(canal, pwm, vel_var.get())

def on_vel_changed(event, canal, vel_var, vel_box):
    vel_txt = vel_var.get()
    vel_box.config(state='normal'); vel_box.delete(0, tk.END); vel_box.insert(0, vel_txt); vel_box.config(state='readonly')

# ======================= COMANDOS MOTORES DC (BTS7960) =======================
def _fmt_pct(pct_text):
    try:
        v = int(pct_text.strip())
    except:
        v = 0
    v = max(0, min(100, v))
    return f"{v:03d}"

def motor_cmd(cual, signo, pct_text):
    if not _serial_ok():
        label_estado.config(text="Puerto no conectado", fg="red")
        return
    if cual not in ('A', 'B') or signo not in ('+', '-'):
        return
    ddd = _fmt_pct(pct_text)
    cmd = f"&{cual}{signo}{ddd}".encode('ascii')
    try:
        with serial_lock:
            puerto_serial.write(cmd)
        label_estado.config(text=f"Motor {cual} ← {signo}{ddd}%", fg="blue")
    except Exception as e:
        messagebox.showerror("Motores", f"Error enviando comando: {e}")

def motores_parada():
    """Enviar &S para detener ambos motores."""
    if not _serial_ok():
        label_estado.config(text="Puerto no conectado", fg="red")
        return
    try:
        with serial_lock:
            puerto_serial.write(b'&S')
        label_estado.config(text="Motores detenidos (&S)", fg="blue")
    except Exception as e:
        messagebox.showerror("Motores", f"Error enviando &S: {e}")

def motores_aplicar_ambos(signoA, signoB, pctA_text, pctB_text):
    """
    Envía &A±DDD y &B±DDD consecutivos dentro del mismo lock
    para que se apliquen 'al mismo tiempo'.
    """
    if not _serial_ok():
        label_estado.config(text="Puerto no conectado", fg="red")
        return
    if signoA not in ('+', '-') or signoB not in ('+', '-'):
        return
    dA = _fmt_pct(pctA_text)
    dB = _fmt_pct(pctB_text)
    try:
        with serial_lock:
            puerto_serial.write(f"&A{signoA}{dA}".encode('ascii'))
            puerto_serial.write(f"&B{signoB}{dB}".encode('ascii'))
        label_estado.config(text=f"Ambos motores aplicados → A {signoA}{dA}% | B {signoB}{dB}%", fg="blue")
    except Exception as e:
        messagebox.showerror("Motores", f"Error aplicando ambos: {e}")

# HOME ($)
def enviar_home():
    """Enviar '$' para ir a Home."""
    if not _serial_ok():
        label_estado.config(text="Puerto no conectado", fg="red")
        return
    try:
        with serial_lock:
            puerto_serial.write(b'$')
        label_estado.config(text="Comando HOME ($) enviado", fg="blue")
    except Exception as e:
        messagebox.showerror("HOME", f"Error enviando $: {e}")

# PARADA INMEDIATA DE SERVOS ('|')
def enviar_parada_servos_servos():
    """Enviar '|' para parada inmediata de servos (E-Stop)."""
    if not _serial_ok():
        label_estado.config(text="Puerto no conectado", fg="red")
        return
    try:
        with serial_lock:
            puerto_serial.write(b'|')
        label_estado.config(text="Parada inmediata de servos (|) enviada", fg="blue")
    except Exception as e:
        messagebox.showerror("Parada servos", f"Error enviando '|': {e}")

# HOME PARCIALES (']' derecho, '%' izquierdo, '\\' cabeza)
def enviar_home_brazo_derecho():
    """Enviar ']' para HOME del brazo derecho (0,1,2,3,4,16)."""
    if not _serial_ok():
        label_estado.config(text="Puerto no conectado", fg="red"); return
    try:
        with serial_lock:
            puerto_serial.write(b']')
        label_estado.config(text="HOME brazo derecho (]) enviado", fg="blue")
    except Exception as e:
        messagebox.showerror("HOME derecho", f"Error enviando ']': {e}")

def enviar_home_brazo_izquierdo():
    """Enviar '%' para HOME del brazo izquierdo (5,6,7,8,9,17)."""
    if not _serial_ok():
        label_estado.config(text="Puerto no conectado", fg="red"); return
    try:
        with serial_lock:
            puerto_serial.write(b'%')
        label_estado.config(text="HOME brazo izquierdo (%) enviado", fg="blue")
    except Exception as e:
        messagebox.showerror("HOME izquierdo", f"Error enviando '%': {e}")

def enviar_home_cabeza():
    """Enviar '\\' para HOME de cabeza (18,19,20)."""
    if not _serial_ok():
        label_estado.config(text="Puerto no conectado", fg="red"); return
    try:
        with serial_lock:
            puerto_serial.write(b'\\')  # backslash escapado
        label_estado.config(text="HOME cabeza (\\) enviado", fg="blue")
    except Exception as e:
        messagebox.showerror("HOME cabeza", f"Error enviando '\\': {e}")

# ======================= COMANDOS PINZAS {R0..4}/{L0..4} =======================
def enviar_pinza_derecha(nivel: int):
    if not _serial_ok():
        label_estado.config(text="Puerto no conectado", fg="red"); return
    nivel = max(0, min(4, int(nivel)))
    cmd = "{R%d}" % nivel
    try:
        with serial_lock:
            puerto_serial.write(cmd.encode('ascii'))
        label_estado.config(text=f"Pinza derecha → {cmd}", fg="blue")
    except Exception as e:
        messagebox.showerror("Pinza derecha", f"Error enviando {cmd}: {e}")

def enviar_pinza_izquierda(nivel: int):
    if not _serial_ok():
        label_estado.config(text="Puerto no conectado", fg="red"); return
    nivel = max(0, min(4, int(nivel)))
    cmd = "{L%d}" % nivel
    try:
        with serial_lock:
            puerto_serial.write(cmd.encode('ascii'))
        label_estado.config(text=f"Pinza izquierda → {cmd}", fg="blue")
    except Exception as e:
        messagebox.showerror("Pinza izquierda", f"Error enviando {cmd}: {e}")

# ======================= COMANDOS DE SENSORES =======================
def medir_FB_instantaneo():
    if not _serial_ok():
        label_estado.config(text="Puerto no conectado", fg="red"); return
    try:
        _monitor_pause(True)
        with serial_lock:
            puerto_serial.reset_input_buffer()
            puerto_serial.write(b'_')
        t0 = time.time()
        buf = b''
        while time.time() - t0 <= RESP_TIMEOUT:
            with serial_lock:
                b = puerto_serial.read(1)
            if b:
                buf += b
                s = buf.decode('ascii', errors='ignore')
                if '_{' in s and '}_' in s:
                    break
            else:
                time.sleep(0.001)
        txt = buf.decode(errors='ignore')
        i = txt.find('_{'); j = txt.find('}_', i+2)
        if i != -1 and j != -1:
            lbl_fb.config(text=f"_{txt[i+2:j]}_", fg="black")
            label_estado.config(text="Medición F/B lista", fg="green")
        else:
            lbl_fb.config(text="(sin datos)", fg="red")
            label_estado.config(text="No se detectó formato _{Fxxxx,Byyyy}_", fg="red")
    except Exception as e:
        messagebox.showerror("Sensores", f"Error: {e}")
    finally:
        _monitor_pause(False)

def escaneo_tilt_minFB():
    if not _serial_ok():
        label_estado.config(text="Puerto no conectado", fg="red"); return
    try:
        label_estado.config(text="Ejecutando escaneo tilt...", fg="blue")
        txt = _send_and_read_until(4, r"\^\{F\d{4},B\d{4}\}\^", 120.0, b'^')
        i = txt.find('^{'); j = txt.find('}^', i+2)
        if i != -1 and j != -1:
            lbl_fb.config(text=f"^{txt[i+2:j]}^", fg="black")
            label_estado.config(text="Escaneo tilt listo", fg="green")
        else:
            lbl_fb.config(text="(sin datos)", fg="red")
            label_estado.config(text="No se detectó formato ^{Fxxxx,Byyyy}^", fg="red")
    except Exception as e:
        messagebox.showerror("Sensores", f"Error: {e}")

def giro_izq_medicion_LR():
    if not _serial_ok():
        label_estado.config(text="Puerto no conectado", fg="red"); return
    try:
        label_estado.config(text="Giro izquierda + medición...", fg="blue")
        txt = _send_and_read_until(2, r"\[\{L\d{4},R\d{4}\}\[", 120.0, b'[')
        i = txt.find('[{'); j = txt.find('}[', i+2)
        if i != -1 and j != -1:
            lbl_lr.config(text=f"[{txt[i+2:j]}]", fg="black")
            label_estado.config(text="Giro izquierda + medición listo", fg="green")
        else:
            lbl_lr.config(text="(sin datos)", fg="red")
            label_estado.config(text="No se detectó formato [{Lxxxx,Rxxxx}[", fg="red")
    except Exception as e:
        messagebox.showerror("Sensores", f"Error: {e}")

# ======================= VENTANA DE POSICIONES =======================
def _ensure_pos_window():
    global pos_win, pos_tree, toggle_pos_btn
    if pos_win and pos_win.winfo_exists():
        try:
            pos_win.deiconify()
            pos_win.lift()
        except:
            pass
        return
    pos_win = tk.Toplevel()
    pos_win.title("Posiciones de Servos")
    pos_win.geometry("380x560")
    pos_win.resizable(True, True)

    cols = ("Canal", "PWM (µs)", "Vel")
    tree = ttk.Treeview(pos_win, columns=cols, show="headings", height=28)
    for c in cols:
        tree.heading(c, text=c)
    tree.column("Canal", width=70, anchor=tk.CENTER)
    tree.column("PWM (µs)", width=120, anchor=tk.CENTER)
    tree.column("Vel", width=80, anchor=tk.CENTER)

    vsb = ttk.Scrollbar(pos_win, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    vsb.pack(side=tk.RIGHT, fill=tk.Y)

    for ch in range(NUM_CANALES):
        tree.insert("", tk.END, iid=f"ch{ch}", values=(f"{ch:02d}", "-", "-"))

    pos_tree = tree

    def _on_close():
        global pos_win, pos_tree, toggle_pos_btn
        pos_tree = None
        if pos_win:
            try:
                pos_win.destroy()
            except:
                pass
        pos_win = None
        if toggle_pos_btn and toggle_pos_btn.winfo_exists():
            toggle_pos_btn.config(text="Mostrar tabla de posiciones")

    pos_win.protocol("WM_DELETE_WINDOW", _on_close)

def _update_pos_window(vistos_dict):
    global pos_tree
    if not (pos_tree and pos_tree.winfo_exists()):
        return
    for ch in range(NUM_CANALES):
        if ch in vistos_dict:
            pwm, vel = vistos_dict[ch]
        else:
            _, pwm_slider, vel_var, *_ = sliders_info[ch]
            pwm = int(round(pwm_slider.get()))
            try:
                vel = int(vel_var.get())
            except:
                vel = 1
        pos_tree.set(f"ch{ch}", column="PWM (µs)", value=str(pwm))
        pos_tree.set(f"ch{ch}", column="Vel", value=f"{vel:02d}")

def toggle_tabla_posiciones():
    global pos_win, toggle_pos_btn
    if pos_win and pos_win.winfo_exists():
        try:
            pos_win.destroy()
        except:
            pass
        pos_win = None
        if toggle_pos_btn and toggle_pos_btn.winfo_exists():
            toggle_pos_btn.config(text="Mostrar tabla de posiciones")
    else:
        _ensure_pos_window()
        if toggle_pos_btn and toggle_pos_btn.winfo_exists():
            toggle_pos_btn.config(text="Ocultar tabla de posiciones")
        leer_posiciones()
        try:
            pos_win.lift()
        except:
            pass

# ======================= LECTURA DE POSICIONES ('.') =======================
def leer_posiciones():
    global updating
    if not _serial_ok():
        messagebox.showwarning("Serial", "Puerto no conectado.")
        _ensure_pos_window()
        _update_pos_window({})
        return
    try:
        _monitor_pause(True)
        with serial_lock:
            puerto_serial.reset_input_buffer()
            puerto_serial.write(b'.')
        t0 = time.time()
        buf = b''
        while time.time() - t0 <= RESP_TIMEOUT:
            with serial_lock:
                chunk = puerto_serial.read(64)
            if chunk:
                buf += chunk
                if len(re.findall(rb"\d{2}\d{4}\d{2}", buf)) >= NUM_CANALES:
                    time.sleep(0.050)
                    with serial_lock:
                        buf += puerto_serial.read(256)
                    break
            else:
                time.sleep(0.002)

        txt = buf.decode('ascii', errors='ignore')
        triples = re.findall(r"(\d{2})(\d{4})(\d{2})", txt)
        if not triples:
            label_estado.config(text="No se detectaron bloques CCPPPPVV", fg="red")
            _ensure_pos_window()
            _update_pos_window({})
            return

        vistos = {}
        for cc, pppp, vv in triples:
            canal = int(cc)
            if 0 <= canal < NUM_CANALES:
                pwm = max(PWM_MIN, min(PWM_MAX, int(pppp)))
                vel = max(1, min(15, int(vv)))
                vistos[canal] = (pwm, vel)

        updating = True
        try:
            for canal, pwm_slider, vel_var, activo_var, pwm_box, vel_box, vel_combo in sliders_info:
                if canal in vistos:
                    pwm, vel = vistos[canal]
                    pwm_slider.set(pwm)
                    pwm_box.config(state='normal'); pwm_box.delete(0, tk.END); pwm_box.insert(0, str(pwm)); pwm_box.config(state='readonly')
                    vel_var.set(f"{vel:02d}")
                    vel_box.config(state='normal'); vel_box.delete(0, tk.END); vel_box.insert(0, f"{vel:02d}"); vel_box.config(state='readonly')
                    vel_combo.set(f"{vel:02d}")
        finally:
            updating = False

        label_estado.config(text="Posiciones leídas y aplicadas", fg="green")
        _ensure_pos_window()
        _update_pos_window(vistos)

    except Exception as e:
        messagebox.showerror("Leer Posiciones", f"Error: {e}")
    finally:
        _monitor_pause(False)

# ======================= ASCII / TERMINAL =======================
def ascii_enviar():
    if not _serial_ok():
        label_estado.config(text="Puerto no conectado", fg="red"); return
    if not ascii_send_text:
        return
    try:
        data = ascii_send_text.get("1.0", tk.END)
        if not data:
            return
        b = data.encode('ascii', errors='replace')
        if len(b) == 0:
            return
        with serial_lock:
            puerto_serial.write(b)
        label_estado.config(text=f"Enviados {len(b)} bytes ASCII", fg="blue")
    except Exception as e:
        messagebox.showerror("ASCII", f"Error enviando: {e}")

def _rx_append(texto):
    if not rx_text:
        return
    rx_text.config(state='normal')
    rx_text.insert(tk.END, texto)
    rx_text.see(tk.END)
    rx_text.config(state='disabled')

def recibir_leer_disponibles():
    if not _serial_ok():
        label_estado.config(text="Puerto no conectado", fg="red"); return
    try:
        with serial_lock:
            n = puerto_serial.in_waiting if hasattr(puerto_serial, 'in_waiting') else 0
            data = puerto_serial.read(n) if n and n > 0 else b''
        if data:
            try:
                s = data.decode('ascii', errors='replace')
            except Exception:
                s = repr(data)
            _rx_append(s)
            label_estado.config(text=f"Recibidos {len(data)} bytes", fg="green")
        else:
            label_estado.config(text="No hay datos disponibles", fg="black")
    except Exception as e:
        messagebox.showerror("RX", f"Error leyendo: {e}")

def recibir_limpiar():
    if not rx_text:
        return
    rx_text.config(state='normal')
    rx_text.delete("1.0", tk.END)
    rx_text.config(state='disabled')

# >>> MONITOR EN VIVO: control
def _monitor_pause(pause: bool):
    if pause:
        _monitor_pause_evt.set()
    else:
        _monitor_pause_evt.clear()

def _monitor_stop():
    _monitor_stop_evt.set()

def _monitor_restart():
    global _monitor_thread
    _monitor_stop()
    time.sleep(0.05)
    _monitor_stop_evt.clear()
    _monitor_pause_evt.clear()
    _monitor_thread = threading.Thread(target=_monitor_loop, daemon=True)
    _monitor_thread.start()

def _monitor_loop():
    while not _monitor_stop_evt.is_set():
        if _monitor_pause_evt.is_set() or not _serial_ok():
            time.sleep(0.03)
            continue
        try:
            with serial_lock:
                n = puerto_serial.in_waiting if hasattr(puerto_serial, 'in_waiting') else 0
                data = puerto_serial.read(n) if n and n > 0 else b''
            if data:
                try:
                    s = data.decode('ascii', errors='replace')
                except Exception:
                    s = repr(data)
                if _root_ref:
                    _root_ref.after(0, _rx_append, s)
            else:
                time.sleep(0.01)
        except Exception:
            time.sleep(0.05)

# ======================= HELPER DE ENVÍO + (OPCIONAL) ESPERA ACK =======================
def _await_ack_or_abort(idx: int, contexto: str) -> bool:
    """Devuelve True si llegó ACK. Si no, marca stop y muestra error."""
    got = _wait_for_ack_star(ACK_TIMEOUT)
    if not got:
        playback_stop_evt.set()
        label_estado.config(text=f"Paso {idx} ({contexto}): no llegó ACK '*'. Reproducción detenida.", fg="red")
        return False
    # pequeña espera para no encimar tramas
    time.sleep(0.01)
    return True

def _send_and_maybe_wait_ack(send_callable, wait_ack: bool, idx: int, contexto: str) -> bool:
    """
    Pausa el monitor RX, limpia el buffer de entrada, ejecuta `send_callable()`,
    y opcionalmente espera el '*' de ACK. Devuelve True si todo OK.
    """
    _monitor_pause(True)
    try:
        # Evitar que un '*' viejo confunda al paso actual
        with serial_lock:
            if _serial_ok():
                try:
                    puerto_serial.reset_input_buffer()
                except:
                    pass

        # Enviar lo que corresponda (el callable hace el write/lock)
        send_callable()

        # Si este paso requiere sincronía, esperamos el ACK del firmware
        if wait_ack:
            if not _await_ack_or_abort(idx, contexto):
                return False

        return True
    finally:
        # breve separación para no encimar tramas sucesivas
        time.sleep(0.02)
        _monitor_pause(False)

# ====== NUEVO: Política de ACK por tipo de paso ======
def _should_wait_ack(step_type: str, global_sync: bool) -> bool:
    """
    Para pasos de motores DC ('motor', 'motors', 'motors_stop') no esperamos ACK,
    incluso si el Sync global está activado. Para el resto, usamos el estado global.
    """
    if step_type in ("motor", "motors", "motors_stop"):
        return False
    return bool(global_sync)

# ====== Helper de texto de pasos (para "Paso → RX") ======
def _step_to_text(step, idx):
    t = step.get("type", "?")
    desc = step.get("desc", "").strip()
    wait = int(step.get("wait_ms", 0))
    sync = "Sí" if bool(step.get("sync", False)) else "No"

    if t == "move":
        items = step.get("items", [])
        parts = [f"Paso {idx}: MOVE  (sync={sync}, espera={wait} ms)"]
        for (ch, pwm, vel) in items:
            parts.append(f"  - ch {ch:02d}: PWM={pwm}  Vel={vel:02d}")
        if desc:
            parts.append(f"  · {desc}")
        return "\n".join(parts)

    if t == "motor":
        which = step.get("which", "A")
        sign  = step.get("sign", "+")
        pct   = int(step.get("pct", 0))
        s = f"Paso {idx}: MOTOR  {which} {sign}{pct:03d}% (sync={sync}, espera={wait} ms)"
        if step.get("autostop", False) and wait > 0:
            s += "  [auto-stop]"
        return s + (f"\n  · {desc}" if desc else "")

    if t == "motors":
        sA = step.get("signA", "+"); pA = int(step.get("pctA", 0))
        sB = step.get("signB", "+"); pB = int(step.get("pctB", 0))
        s = f"Paso {idx}: MOTORS  A {sA}{pA:03d}% / B {sB}{pB:03d}% (sync={sync}, espera={wait} ms)"
        if step.get("autostop", False) and wait > 0:
            s += "  [auto-stop]"
        return s + (f"\n  · {desc}" if desc else "")

    if t == "motors_stop":
        s = f"Paso {idx}: PARADA (&S) (sync={sync}, espera={wait} ms)"
        return s + (f"\n  · {desc}" if desc else "")

    if t == "grip":
        side = step.get("side", "R"); level = int(step.get("level", 1))
        s = f"Paso {idx}: GRIP  {side}{level} (sync={sync}, espera={wait} ms)"
        return s + (f"\n  · {desc}" if desc else "")

    if t == "grips":
        lr = int(step.get("levelR", 1)); ll = int(step.get("levelL", 1))
        s = f"Paso {idx}: GRIPS  R{lr} / L{ll} (sync={sync}, espera={wait} ms)"
        return s + (f"\n  · {desc}" if desc else "")

    return f"Paso {idx}: <tipo desconocido> {t}"

# ======================= SECUENCIAS =======================
def _seq_clear_running():
    """Quita el resaltado azul actual (si hubiese)."""
    if not seq_tree:
        return
    def _do():
        global _seq_running_iid
        if _seq_running_iid and seq_tree.exists(_seq_running_iid):
            seq_tree.item(_seq_running_iid, tags=tuple(t for t in seq_tree.item(_seq_running_iid, "tags") if t != "RUN"))
        _seq_running_iid = None
    if _root_ref:
        _root_ref.after(0, _do)

def _seq_mark_running(idx: int):
    """Resalta en azul la fila 'idx' (1-based) mientras se ejecuta."""
    if not seq_tree:
        return
    iid = f"s{idx}"
    def _do():
        global _seq_running_iid
        if not (seq_tree and seq_tree.exists(iid)):
            return
        # limpiar anterior
        if _seq_running_iid and seq_tree.exists(_seq_running_iid):
            seq_tree.item(_seq_running_iid, tags=tuple(t for t in seq_tree.item(_seq_running_iid, "tags") if t != "RUN"))
        # asegurar estilo y aplicar
        seq_tree.tag_configure("RUN", background="#CFE8FF")  # azul claro
        current_tags = set(seq_tree.item(iid, "tags"))
        current_tags.add("RUN")
        seq_tree.item(iid, tags=tuple(current_tags))
        seq_tree.see(iid)   # hacer scroll hasta la fila
        _seq_running_iid = iid
    if _root_ref:
        _root_ref.after(0, _do)

def _seq_refresh_tree():
    # (opcional) limpiar resaltado si se reconstuye la tabla
    _seq_clear_running()  # <<< NUEVO (opcional seguro)
    if not seq_tree:
        return
    for i in seq_tree.get_children():
        seq_tree.delete(i)
    for idx, step in enumerate(secuencia, start=1):
        tag = f"[{step.get('group')}] " if step.get('group') else ""  # prefijo de grupo

        if step["type"] == "move":
            n = len(step["items"])
            sync = "Sí" if step.get("sync", False) else "No"
            wait = step.get("wait_ms", 0)
            desc = step.get("desc", "").strip()
            detalle_txt = f"{tag}{n} ch" + (f" — {desc}" if desc else "")
            seq_tree.insert("", tk.END, iid=f"s{idx}",
                            values=(idx, "Mover", detalle_txt, sync, f"{wait} ms"))
        elif step["type"] == "wait":
            ms = step.get("wait_ms", 0)
            desc = step.get("desc", "").strip()
            detalle_txt = f"{tag}-" + (f" — {desc}" if desc else "")
            seq_tree.insert("", tk.END, iid=f"s{idx}",
                            values=(idx, "Esperar", detalle_txt, "-", f"{ms} ms"))
        elif step["type"] == "grip":
            side = step.get("side", "R")
            level = int(step.get("level", 1))
            sync = "Sí" if step.get("sync", False) else "No"
            wait = int(step.get("wait_ms", 0))
            desc = step.get("desc", "").strip()
            detalle_txt = f"{tag}Pinza {side}{level}" + (f" — {desc}" if desc else "")
            seq_tree.insert("", tk.END, iid=f"s{idx}",
                            values=(idx, "Pinza", detalle_txt, sync, f"{wait} ms"))
        elif step["type"] == "grips":
            lr = int(step.get("levelR", 1))
            ll = int(step.get("levelL", 1))
            sync = "Sí" if step.get("sync", False) else "No"
            wait = int(step.get("wait_ms", 0))
            desc = step.get("desc", "").strip()
            detalle_txt = f"{tag}Pinzas R{lr} / L{ll}" + (f" — {desc}" if desc else "")
            seq_tree.insert("", tk.END, iid=f"s{idx}",
                            values=(idx, "Pinzas", detalle_txt, sync, f"{wait} ms"))
        elif step["type"] == "motor":
            which = step.get("which", "A")
            sign = step.get("sign", "+")
            pct = int(step.get("pct", 0))
            pct = max(0, min(100, pct))
            sync = "Sí" if step.get("sync", False) else "No"
            wait = int(step.get("wait_ms", 0))
            desc = step.get("desc", "").strip()
            detalle_txt = f"{tag}Motor {which} {sign}{pct:03d}%" + (f" — {desc}" if desc else "")
            if step.get("autostop", False) and wait > 0:
                detalle_txt += " [auto-stop]"
            seq_tree.insert("", tk.END, iid=f"s{idx}",
                            values=(idx, "Motor", detalle_txt, sync, f"{wait} ms"))
        elif step["type"] == "motors":
            signA = step.get("signA", "+"); pctA = int(step.get("pctA", 0))
            signB = step.get("signB", "+"); pctB = int(step.get("pctB", 0))
            pctA = max(0, min(100, pctA)); pctB = max(0, min(100, pctB))
            sync = "Sí" if step.get("sync", False) else "No"
            wait = int(step.get("wait_ms", 0))
            desc = step.get("desc", "").strip()
            detalle_txt = f"{tag}A {signA}{pctA:03d}% / B {signB}{pctB:03d}%" + (f" — {desc}" if desc else "")
            if step.get("autostop", False) and wait > 0:
                detalle_txt += " [auto-stop]"
            seq_tree.insert("", tk.END, iid=f"s{idx}",
                            values=(idx, "Motores", detalle_txt, sync, f"{wait} ms"))
        elif step["type"] == "motors_stop":
            sync = "Sí" if step.get("sync", False) else "No"
            wait = int(step.get("wait_ms", 0))
            desc = step.get("desc", "").strip()
            detalle_txt = f"{tag}Parada (&S)" + (f" — {desc}" if desc else "")
            seq_tree.insert("", tk.END, iid=f"s{idx}",
                            values=(idx, "Motores", detalle_txt, sync, f"{wait} ms"))

def _seq_capture_from_sliders():
    """Captura un paso de 'move' desde sliders ACTIVOS."""
    items = []
    for canal, pwm_slider, vel_var, activo_var, *_ in sliders_info:
        if activo_var.get():
            pwm = int(round(pwm_slider.get()))
            vel = int(vel_var.get())
            items.append((canal, pwm, vel))
    if not items:
        messagebox.showinfo("Secuencia", "No hay canales activos para capturar.")
        return
    try:
        wait_ms = int(seq_wait_entry.get().strip()) if seq_wait_entry else 0
    except:
        wait_ms = 0
    step = {"type": "move", "items": items, "sync": bool(seq_sync_var.get()),
            "wait_ms": max(0, wait_ms), "desc": ""}
    secuencia.append(step)
    _seq_refresh_tree()

def _seq_add_wait():
    """Agrega un paso de espera (ms)."""
    try:
        wait_ms = int(seq_wait_entry.get().strip()) if seq_wait_entry else 0
    except:
        wait_ms = 0
    secuencia.append({"type": "wait", "wait_ms": max(0, wait_ms), "desc": ""})
    _seq_refresh_tree()

# --- funciones de pinza (una/ambas) ---
def _seq_add_grip(side: str, level: int):
    """Agrega un paso de Pinza: side in {'R','L'}, level in 0..4."""
    s = (side or 'R').upper()
    if s not in ('R', 'L'):
        s = 'R'
    try:
        lvl = int(level)
    except:
        lvl = 1
    lvl = max(0, min(4, lvl))
    try:
        wait_ms = int(seq_wait_entry.get().strip()) if seq_wait_entry else 0
    except:
        wait_ms = 0
    step = {
        "type": "grip",
        "side": s,
        "level": lvl,
        "sync": bool(seq_sync_var.get()),
        "wait_ms": max(0, wait_ms),
        "desc": "",
    }
    secuencia.append(step)
    _seq_refresh_tree()

def _seq_add_grips(level_r: int, level_l: int):
    """Agrega un paso que opera ambas pinzas a la vez (niveles 0..4)."""
    try:
        lr = int(level_r)
    except:
        lr = 1
    try:
        ll = int(level_l)
    except:
        ll = 1
    lr = max(0, min(4, lr))
    ll = max(0, min(4, ll))
    try:
        wait_ms = int(seq_wait_entry.get().strip()) if seq_wait_entry else 0
    except:
        wait_ms = 0
    step = {
        "type": "grips",
        "levelR": lr,
        "levelL": ll,
        "sync": bool(seq_sync_var.get()),
        "wait_ms": max(0, wait_ms),
        "desc": "",
    }
    secuencia.append(step)
    _seq_refresh_tree()

# --- funciones de motores (uno/ambos) ---
def _seq_add_motor(which: str, sign: str, pct: int):
    """Agrega un paso para un motor A/B con dirección +/− y % 0..100."""
    w = (which or 'A').upper()
    if w not in ('A','B'):
        w = 'A'
    s = sign if sign in ('+','-') else '+'
    try:
        p = int(pct)
    except:
        p = 0
    p = max(0, min(100, p))
    try:
        wait_ms = int(seq_wait_entry.get().strip()) if seq_wait_entry else 0
    except:
        wait_ms = 0
    step = {
        "type": "motor",
        "which": w,
        "sign": s,
        "pct": p,
        "sync": bool(seq_sync_var.get()),
        "wait_ms": max(0, wait_ms),
        "autostop": bool(motors_autostop_var.get()),   # sincronizar autostop
        "desc": "",
    }
    secuencia.append(step)
    _seq_refresh_tree()

def _seq_add_motors(signA: str, pctA: int, signB: str, pctB: int):
    """Agrega un paso para **ambos** motores con direcciones y % independientes."""
    sA = signA if signA in ('+','-') else '+'
    sB = signB if signB in ('+','-') else '+'

    try:
        pA = int(pctA)
    except:
        pA = 0
    try:
        pB = int(pctB)
    except:
        pB = 0

    pA = max(0, min(100, pA))
    pB = max(0, min(100, pB))

    try:
        wait_ms = int(seq_wait_entry.get().strip()) if seq_wait_entry else 0
    except:
        wait_ms = 0

    step = {
        "type": "motors",
        "signA": sA,
        "pctA": pA,
        "signB": sB,
        "pctB": pB,
        "sync": bool(seq_sync_var.get()),
        "wait_ms": max(0, wait_ms),
        "autostop": bool(motors_autostop_var.get()),
        "desc": "",
    }
    secuencia.append(step)
    _seq_refresh_tree()

def _seq_add_motors_stop():
    """Agrega un paso que envía &S (parada de ambos motores)."""
    try:
        wait_ms = int(seq_wait_entry.get().strip()) if seq_wait_entry else 0
    except:
        wait_ms = 0
    step = {
        "type": "motors_stop",
        "sync": bool(seq_sync_var.get()),  # visible en tabla; reproducción no espera ACK
        "wait_ms": max(0, wait_ms),
        "desc": "",
    }
    secuencia.append(step)
    _seq_refresh_tree()

# --- utilidades de selección ---
def _get_selected_index():
    sel = seq_tree.selection()
    if not sel:
        return None
    try:
        idx = int(seq_tree.set(sel[0], "N°")) - 1
    except:
        return None
    if not (0 <= idx < len(secuencia)):
        return None
    return idx

def _get_selected_indices():
    sels = seq_tree.selection()
    if not sels:
        return []
    idxs = []
    for rid in sels:
        try:
            i = int(seq_tree.set(rid, "N°")) - 1
            if 0 <= i < len(secuencia):
                idxs.append(i)
        except:
            pass
    # ordenar y deduplicar
    return sorted(set(idxs))

# --------- Mover ↑/↓ con selección múltiple (un paso por pulsación) ----------
def _seq_move_up():
    idxs = _get_selected_indices()
    if not idxs:
        return
    n = len(secuencia)
    selected = [False]*n
    for i in idxs:
        selected[i] = True
    for j in range(1, n):
        if selected[j] and not selected[j-1]:
            secuencia[j-1], secuencia[j] = secuencia[j], secuencia[j-1]
            selected[j-1], selected[j] = selected[j], selected[j-1]
    _seq_refresh_tree()
    new_idxs = [i for i, s in enumerate(selected) if s]
    seq_tree.selection_set([f"s{i+1}" for i in new_idxs])

def _seq_move_down():
    idxs = _get_selected_indices()
    if not idxs:
        return
    n = len(secuencia)
    selected = [False]*n
    for i in idxs:
        selected[i] = True
    for j in range(n-2, -1, -1):
        if selected[j] and not selected[j+1]:
            secuencia[j], secuencia[j+1] = secuencia[j+1], secuencia[j]
            selected[j], selected[j+1] = selected[j+1], selected[j]
    _seq_refresh_tree()
    new_idxs = [i for i, s in enumerate(selected) if s]
    seq_tree.selection_set([f"s{i+1}" for i in new_idxs])
# -----------------------------------------------------------------------------

def _seq_delete():
    idxs = _get_selected_indices()
    if not idxs:
        return
    for i in reversed(idxs):
        del secuencia[i]
    _seq_refresh_tree()

def _seq_clear():
    if secuencia:
        if not messagebox.askyesno("Secuencia", "¿Limpiar toda la secuencia?"):
            return
    secuencia.clear()
    _seq_refresh_tree()

def _ensure_sequences_dir():
    """Crea (si no existe) y devuelve la carpeta 'secuencias' en el directorio actual."""
    folder = os.path.join(os.getcwd(), "secuencias")
    os.makedirs(folder, exist_ok=True)
    return folder

# ===== GATILLO POR RECONOCIMIENTO FACIAL =====
# Un script aparte (reconocimiento_facial.py) escribe el nombre de la
# acción (ej: "saludo" o "pelea") en este archivo. Acá lo leemos por
# polling (cada 500 ms, en el hilo principal de Tkinter) y reproducimos
# la secuencia correspondiente reusando el mismo motor que usa la voz.
TRIGGER_FILE = os.path.join(os.getcwd(), "gatillo_facial.txt")

def _cargar_y_reproducir_secuencia(nombre):
    """Busca ./secuencias/<nombre>.json, la carga y la reproduce."""
    try:
        base_dir = _ensure_sequences_dir()
        fname = os.path.join(base_dir, f"{nombre}.json")
        if os.path.exists(fname):
            _seq_load_from_file(fname)
            try:
                _seq_play()
            except Exception as e:
                try:
                    label_estado.config(text=f"Error al ejecutar '{nombre}': {e}", fg="red")
                except Exception:
                    pass
        else:
            try:
                label_estado.config(text=f"No encontré 'secuencias/{nombre}.json'", fg="orange")
            except Exception:
                pass
    except Exception as e:
        try:
            label_estado.config(text=f"Error preparando '{nombre}': {e}", fg="red")
        except Exception:
            pass

def _check_trigger_facial():
    """Poll periódico (hilo principal): si aparece gatillo_facial.txt, dispara la secuencia."""
    try:
        if os.path.exists(TRIGGER_FILE):
            with open(TRIGGER_FILE, "r", encoding="utf-8") as f:
                nombre = f.read().strip()
            os.remove(TRIGGER_FILE)
            if nombre:
                _cargar_y_reproducir_secuencia(nombre)
    except Exception:
        pass
    finally:
        if _root_ref is not None:
            _root_ref.after(500, _check_trigger_facial)

def _seq_save_json():
    """Guardar como... (permite nombrar y elegir ubicación; por defecto en ./secuencias)."""
    try:
        data = {"version": 1, "steps": secuencia}
        base_dir = _ensure_sequences_dir()
        fname = filedialog.asksaveasfilename(
            parent=_root_ref,
            title="Guardar secuencia como...",
            initialdir=base_dir,
            initialfile="secuencia.json",
            defaultextension=".json",
            filetypes=[("Archivos JSON", "*.json")],
        )
        if not fname:
            return  # cancelado
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        label_estado.config(text=f"Secuencia guardada: {os.path.basename(fname)}", fg="green")
    except Exception as e:
        messagebox.showerror("Guardar", f"Error guardando: {e}")


def _seq_load_from_file(fname):
    """Cargar desde ruta absoluta/relativa (anexa al final), sin abrir diálogos."""
    try:
        if not fname:
            return
        with open(fname, "r", encoding="utf-8") as f:
            data = json.load(f)
        seq_name = os.path.splitext(os.path.basename(fname))[0]
        steps = data.get("steps", [])
        if not isinstance(steps, list):
            messagebox.showerror("Cargar", "El archivo no contiene una lista válida de pasos.")
            return
        added = 0
        for st in steps:
            if st.get("type") == "move" and isinstance(st.get("items"), list):
                secuencia.append({
                    "type": "move",
                    "items": [(int(c), int(p), int(v)) for (c,p,v) in st["items"]],
                    "sync": bool(st.get("sync", False)),
                    "wait_ms": max(0, int(st.get("wait_ms", 0))),
                    "desc": str(st.get("desc", "")),
                    "group": seq_name,
                }); added += 1
            elif st.get("type") == "wait":
                secuencia.append({
                    "type": "wait",
                    "wait_ms": max(0, int(st.get("wait_ms", 0))),
                    "desc": str(st.get("desc", "")),
                    "group": seq_name,
                }); added += 1
            elif st.get("type") == "grip":
                side = str(st.get("side", "R")).upper()
                if side not in ("R", "L"): side = "R"
                try: lvl = int(st.get("level", 1))
                except: lvl = 1
                lvl = max(0, min(4, lvl))
                secuencia.append({
                    "type": "grip",
                    "side": side,
                    "level": lvl,
                    "sync": bool(st.get("sync", False)),
                    "wait_ms": max(0, int(st.get("wait_ms", 0))),
                    "desc": str(st.get("desc", "")),
                    "group": seq_name,
                }); added += 1
            elif st.get("type") == "grips":
                try: lr = int(st.get("levelR", 1))
                except: lr = 1
                try: ll = int(st.get("levelL", 1))
                except: ll = 1
                lr = max(0, min(4, lr)); ll = max(0, min(4, ll))
                secuencia.append({
                    "type": "grips",
                    "levelR": lr,
                    "levelL": ll,
                    "sync": bool(st.get("sync", False)),
                    "wait_ms": max(0, int(st.get("wait_ms", 0))),
                    "desc": str(st.get("desc", "")),
                    "group": seq_name,
                }); added += 1
            elif st.get("type") == "motor":
                sign = st.get("sign", "+")
                if sign not in ('+','-'): sign = '+'
                try: pct = int(st.get("pct", 0))
                except: pct = 0
                pct = max(0, min(100, pct))
                secuencia.append({
                    "type": "motor",
                    "sign": sign,
                    "pct": pct,
                    "sync": bool(st.get("sync", False)),
                    "wait_ms": max(0, int(st.get("wait_ms", 0))),
                    "autostop": bool(st.get("autostop", False)),
                    "desc": str(st.get("desc", "")),
                    "group": seq_name,
                }); added += 1
            elif st.get("type") == "motors":
                signA = st.get("signA", "+"); signB = st.get("signB", "+")
                if signA not in ('+','-'): signA = '+'
                if signB not in ('+','-'): signB = '+'
                try: pctA = int(st.get("pctA", 0))
                except: pctA = 0
                try: pctB = int(st.get("pctB", 0))
                except: pctB = 0
                pctA = max(0, min(100, pctA)); pctB = max(0, min(100, pctB))
                secuencia.append({
                    "type": "motors",
                    "signA": signA,
                    "pctA": pctA,
                    "signB": signB,
                    "pctB": pctB,
                    "sync": bool(st.get("sync", False)),
                    "wait_ms": max(0, int(st.get("wait_ms", 0))),
                    "autostop": bool(st.get("autostop", False)),
                    "desc": str(st.get("desc", "")),
                    "group": seq_name,
                }); added += 1
            elif st.get("type") == "motors_stop":
                secuencia.append({
                    "type": "motors_stop",
                    "desc": str(st.get("desc", "")),
                    "group": seq_name,
                }); added += 1
            else:
                # tipos desconocidos se ignoran
                pass

        _seq_refresh_tree()
        try:
            if added > 0:
                label_estado.config(
                    text=f"Cargado y anexado: {os.path.basename(fname)} — +{added} paso(s) (total {len(secuencia)})",
                    fg="green"
                )
            else:
                label_estado.config(text=f"Archivo cargado sin pasos válidos: {os.path.basename(fname)}", fg="orange")
        except Exception:
            pass
    except Exception as e:
        try:
            messagebox.showerror("Cargar", f"Error cargando: {e}")
        except Exception:
            pass
def _seq_load_json():
    """Cargar... (anexa al final de la secuencia actual)."""
    try:
        base_dir = _ensure_sequences_dir()
        fname = filedialog.askopenfilename(
            parent=_root_ref,
            title="Cargar secuencia...",
            initialdir=base_dir,
            defaultextension=".json",
            filetypes=[("Archivos JSON", "*.json")],
        )
        if not fname:
            return  # cancelado

        with open(fname, "r", encoding="utf-8") as f:
            data = json.load(f)

        # nombre base del archivo sin extensión para etiquetar los pasos
        seq_name = os.path.splitext(os.path.basename(fname))[0]

        steps = data.get("steps", [])
        if not isinstance(steps, list):
            messagebox.showerror("Cargar", "El archivo no contiene una lista válida de pasos.")
            return

        added = 0
        for st in steps:
            if st.get("type") == "move" and isinstance(st.get("items"), list):
                secuencia.append({
                    "type": "move",
                    "items": [(int(c), int(p), int(v)) for (c,p,v) in st["items"]],
                    "sync": bool(st.get("sync", False)),
                    "wait_ms": max(0, int(st.get("wait_ms", 0))),
                    "desc": str(st.get("desc", "")),
                    "group": seq_name,
                }); added += 1
            elif st.get("type") == "wait":
                secuencia.append({
                    "type": "wait",
                    "wait_ms": max(0, int(st.get("wait_ms", 0))),
                    "desc": str(st.get("desc", "")),
                    "group": seq_name,
                }); added += 1
            elif st.get("type") == "grip":
                side = str(st.get("side", "R")).upper()
                if side not in ("R", "L"): side = "R"
                try: lvl = int(st.get("level", 1))
                except: lvl = 1
                lvl = max(0, min(4, lvl))
                secuencia.append({
                    "type": "grip",
                    "side": side,
                    "level": lvl,
                    "sync": bool(st.get("sync", False)),
                    "wait_ms": max(0, int(st.get("wait_ms", 0))),
                    "desc": str(st.get("desc", "")),
                    "group": seq_name,
                }); added += 1
            elif st.get("type") == "grips":
                try: lr = int(st.get("levelR", 1))
                except: lr = 1
                try: ll = int(st.get("levelL", 1))
                except: ll = 1
                lr = max(0, min(4, lr)); ll = max(0, min(4, ll))
                secuencia.append({
                    "type": "grips",
                    "levelR": lr,
                    "levelL": ll,
                    "sync": bool(st.get("sync", False)),
                    "wait_ms": max(0, int(st.get("wait_ms", 0))),
                    "desc": str(st.get("desc", "")),
                    "group": seq_name,
                }); added += 1
            elif st.get("type") == "motor":
                which = str(st.get("which", "A")).upper()
                if which not in ("A","B"): which = "A"
                sign = st.get("sign", "+"); sign = sign if sign in ('+','-') else '+'
                try: pct = int(st.get("pct", 0))
                except: pct = 0
                pct = max(0, min(100, pct))
                secuencia.append({
                    "type": "motor",
                    "which": which,
                    "sign": sign,
                    "pct": pct,
                    "sync": bool(st.get("sync", False)),
                    "wait_ms": max(0, int(st.get("wait_ms", 0))),
                    "autostop": bool(st.get("autostop", False)),
                    "desc": str(st.get("desc", "")),
                    "group": seq_name,
                }); added += 1
            elif st.get("type") == "motors":
                signA = st.get("signA", "+"); signB = st.get("signB", "+")
                if signA not in ('+','-'): signA = '+'
                if signB not in ('+','-'): signB = '+'
                try: pctA = int(st.get("pctA", 0))
                except: pctA = 0
                try: pctB = int(st.get("pctB", 0))
                except: pctB = 0
                pctA = max(0, min(100, pctA)); pctB = max(0, min(100, pctB))
                secuencia.append({
                    "type": "motors",
                    "signA": signA,
                    "pctA": pctA,
                    "signB": signB,
                    "pctB": pctB,
                    "sync": bool(st.get("sync", False)),
                    "wait_ms": max(0, int(st.get("wait_ms", 0))),
                    "autostop": bool(st.get("autostop", False)),
                    "desc": str(st.get("desc", "")),
                    "group": seq_name,
                }); added += 1
            elif st.get("type") == "motors_stop":
                secuencia.append({
                    "type": "motors_stop",
                    "sync": bool(st.get("sync", False)),
                    "wait_ms": max(0, int(st.get("wait_ms", 0))),
                    "desc": str(st.get("desc", "")),
                    "group": seq_name,
                }); added += 1

        _seq_refresh_tree()
        if added > 0:
            label_estado.config(
                text=f"Cargado y anexado: {os.path.basename(fname)} — +{added} paso(s) (total {len(secuencia)})",
                fg="green"
            )
        else:
            label_estado.config(text=f"Archivo cargado sin pasos válidos: {os.path.basename(fname)}", fg="orange")

    except Exception as e:
        messagebox.showerror("Cargar", f"Error cargando: {e}")

# ===== REPRODUCCIÓN =====
def _seq_play_worker():
    try:
        use_global_sync = True
        global_sync = bool(seq_sync_var.get()) if seq_sync_var else False

        for idx, step in enumerate(secuencia, start=1):
            if playback_stop_evt.is_set():
                break

            _seq_mark_running(idx)  # <<< NUEVO: resaltar el paso actual

            step_type = step.get("type")
            want_sync = _should_wait_ack(step_type, global_sync) if use_global_sync else bool(step.get("sync", False))

            if step_type == "move":
                items = step["items"]
                ok = _send_and_maybe_wait_ack(
                    lambda: enviar_paquete_multiple(items),
                    wait_ack=want_sync,
                    idx=idx,
                    contexto="Mover"
                )
                if not ok:
                    break
                local_ms = int(step.get("wait_ms", 0))
                if local_ms > 0:
                    t_end = time.time() + (local_ms/1000.0)
                    while time.time() < t_end:
                        if playback_stop_evt.is_set(): break
                        time.sleep(0.01)

            elif step_type == "wait":
                ms = int(step.get("wait_ms", 0))
                t_end = time.time() + (ms/1000.0)
                while time.time() < t_end:
                    if playback_stop_evt.is_set(): break
                    time.sleep(0.01)

            elif step_type == "grip":
                side = step.get("side", "R")
                level = int(step.get("level", 1)); level = max(0, min(4, level))
                def _send_grip():
                    with serial_lock:
                        puerto_serial.write(f"{{{side}{level}}}".encode('ascii'))
                ok = _send_and_maybe_wait_ack(_send_grip, want_sync, idx, f"Pinza {side}{level}")
                if not ok: break
                local_ms = int(step.get("wait_ms", 0))
                if local_ms > 0:
                    t_end = time.time() + (local_ms/1000.0)
                    while time.time() < t_end:
                        if playback_stop_evt.is_set(): break
                        time.sleep(0.01)

            elif step_type == "grips":
                lr = int(step.get("levelR", 1)); lr = max(0, min(4, lr))
                ll = int(step.get("levelL", 1)); ll = max(0, min(4, ll))
                def _send_grips():
                    with serial_lock:
                        puerto_serial.write(f"{{R{lr}}}".encode('ascii'))
                        puerto_serial.write(f"{{L{ll}}}".encode('ascii'))
                ok = _send_and_maybe_wait_ack(_send_grips, want_sync, idx, f"Pinzas R{lr}/L{ll}")
                if not ok: break
                local_ms = int(step.get("wait_ms", 0))
                if local_ms > 0:
                    t_end = time.time() + (local_ms/1000.0)
                    while time.time() < t_end:
                        if playback_stop_evt.is_set(): break
                        time.sleep(0.01)

            elif step_type == "motor":
                which = step.get("which", 'A')
                sign = step.get("sign", '+')
                pct = int(step.get("pct", 0)); pct = max(0, min(100, pct))
                def _send_motor():
                    with serial_lock:
                        puerto_serial.write(f"&{which}{sign}{pct:03d}".encode('ascii'))
                ok = _send_and_maybe_wait_ack(_send_motor, want_sync, idx, f"Motor {which}")
                if not ok: break
                local_ms = int(step.get("wait_ms", 0))
                if local_ms > 0:
                    t_end = time.time() + (local_ms/1000.0)
                    while time.time() < t_end:
                        if playback_stop_evt.is_set(): break
                        time.sleep(0.01)
                # Auto-parada (&S) si corresponde
                if not playback_stop_evt.is_set() and step.get("autostop", False) and local_ms > 0:
                    def _send_stop():
                        with serial_lock:
                            puerto_serial.write(b'&S')
                    _send_and_maybe_wait_ack(_send_stop, wait_ack=False, idx=idx, contexto="Parada (&S) auto")

            elif step_type == "motors":
                signA = step.get("signA", '+'); pctA = int(step.get("pctA", 0)); pctA = max(0, min(100, pctA))
                signB = step.get("signB", '+'); pctB = int(step.get("pctB", 0)); pctB = max(0, min(100, pctB))
                def _send_motors():
                    with serial_lock:
                        puerto_serial.write(f"&A{signA}{pctA:03d}".encode('ascii'))
                        puerto_serial.write(f"&B{signB}{pctB:03d}".encode('ascii'))
                ok = _send_and_maybe_wait_ack(_send_motors, want_sync, idx, "Motores")
                if not ok: break
                local_ms = int(step.get("wait_ms", 0))
                if local_ms > 0:
                    t_end = time.time() + (local_ms/1000.0)
                    while time.time() < t_end:
                        if playback_stop_evt.is_set(): break
                        time.sleep(0.01)
                # Auto-parada (&S) si corresponde
                if not playback_stop_evt.is_set() and step.get("autostop", False) and local_ms > 0:
                    def _send_stop():
                        with serial_lock:
                            puerto_serial.write(b'&S')
                    _send_and_maybe_wait_ack(_send_stop, wait_ack=False, idx=idx, contexto="Parada (&S) auto")

            elif step_type == "motors_stop":
                def _send_stop():
                    with serial_lock:
                        puerto_serial.write(b'&S')
                ok = _send_and_maybe_wait_ack(_send_stop, want_sync, idx, "Parada (&S)")
                if not ok: break
                local_ms = int(step.get("wait_ms", 0))
                if local_ms > 0:
                    t_end = time.time() + (local_ms/1000.0)
                    while time.time() < t_end:
                        if playback_stop_evt.is_set(): break
                        time.sleep(0.01)

        if playback_stop_evt.is_set():
            label_estado.config(text="Reproducción detenida", fg="red")
        else:
            label_estado.config(text="Reproducción finalizada", fg="green")
        _seq_clear_running()  # <<< NUEVO
    except Exception as e:
        label_estado.config(text=f"Error reproducción: {e}", fg="red")
        _seq_clear_running()  # <<< NUEVO

def _seq_play():
    if not secuencia:
        messagebox.showinfo("Secuencia", "No hay pasos para reproducir.")
        return
    if not _serial_ok():
        messagebox.showwarning("Serial", "Puerto no conectado.")
        return
    if _is_playing():
        return
    playback_stop_evt.clear()
    t = threading.Thread(target=_seq_play_worker, daemon=True)
    t.start()
    globals()["playback_thread"] = t
    label_estado.config(text="Reproduciendo secuencia...", fg="blue")

def _seq_stop():
    if not _is_playing():
        return
    playback_stop_evt.set()
    _seq_clear_running()  # <<< NUEVO
    label_estado.config(text="Deteniendo secuencia...", fg="red")

def _is_playing():
    t = globals().get("playback_thread")
    return t is not None and t.is_alive()

def _seq_on_double_click(event):
    """Editar 'Detalle' con doble click sobre la columna 'Detalle'."""
    if not seq_tree:
        return
    region = seq_tree.identify("region", event.x, event.y)
    if region != "cell":
        return
    col = seq_tree.identify_column(event.x)  # "#1", "#2", "#3", ...
    if col != "#3":  # "Detalle" es la tercera columna
        return
    row_id = seq_tree.identify_row(event.y)
    if not row_id:
        return
    try:
        idx = int(seq_tree.set(row_id, "N°")) - 1
    except:
        return
    if not (0 <= idx < len(secuencia)):
        return
    actual = str(secuencia[idx].get("desc", "")).strip()
    nuevo = simpledialog.askstring("Detalle de la captura",
                                   "Escribí una descripción para este paso:",
                                   initialvalue=actual,
                                   parent=seq_tree)
    if nuevo is None:
        return  # cancelado
    secuencia[idx]["desc"] = nuevo.strip()
    _seq_refresh_tree()

# ---- copiar / pegar / duplicar (con soporte múltiple) ----
def _seq_copy():
    """Copia uno o varios pasos seleccionados (en orden)."""
    global _seq_clip
    idxs = _get_selected_indices()
    if not idxs:
        return
    block = [copy.deepcopy(secuencia[i]) for i in idxs]
    _seq_clip = block
    label_estado.config(text=f"Copiado(s): {len(block)} paso(s)", fg="blue")

def _seq_paste():
    """Pega el bloque copiado después del último seleccionado (o al final)."""
    global _seq_clip
    if not _seq_clip:
        messagebox.showinfo("Pegar", "No hay pasos copiados.")
        return
    idxs = _get_selected_indices()
    insert_at = (max(idxs) + 1) if idxs else len(secuencia)
    for offset, step in enumerate(_seq_clip):
        secuencia.insert(insert_at + offset, copy.deepcopy(step))
    _seq_refresh_tree()
    nuevos = [f"s{insert_at + i + 1}" for i in range(len(_seq_clip))]
    try:
        seq_tree.selection_set(nuevos)
    except:
        pass
    label_estado.config(text=f"Pegado(s): {len(_seq_clip)} paso(s)", fg="green")

def _seq_duplicate():
    """Duplica el primer seleccionado (atajo rápido)."""
    idx = _get_selected_index()
    if idx is None:
        return
    secuencia.insert(idx + 1, copy.deepcopy(secuencia[idx]))
    _seq_refresh_tree()
    seq_tree.selection_set(f"s{idx+2}")
    label_estado.config(text="Paso duplicado", fg="green")

# -------- Nuevas funciones: Paso → RX / Cargar controles / Reemplazar --------
def _seq_dump_selected_to_rx():
    idx = _get_selected_index()
    if idx is None:
        messagebox.showinfo("Secuencia", "Seleccioná un paso primero.")
        return
    step = secuencia[idx]
    txt = _step_to_text(step, idx+1) + "\n"
    _rx_append(txt)
    label_estado.config(text=f"Volcado paso {idx+1} → RX", fg="blue")

def _seq_run_selected():
    """Ejecuta SOLO el paso seleccionado, respetando wait_ms y autostop."""
    idx = _get_selected_index()
    if idx is None:
        messagebox.showinfo("Secuencia", "Seleccioná un paso primero.")
        return
    if not _serial_ok():
        messagebox.showwarning("Serial", "Puerto no conectado.")
        return

    step = secuencia[idx]
    step_type = step.get("type")
    global_sync = bool(seq_sync_var.get()) if seq_sync_var else False
    want_sync = _should_wait_ack(step_type, global_sync)

    try:
        _seq_mark_running(idx+1)  # <<< NUEVO: resaltar también en ejecución individual

        if step_type == "move":
            items = step["items"]
            ok = _send_and_maybe_wait_ack(lambda: enviar_paquete_multiple(items),
                                          wait_ack=want_sync, idx=idx+1, contexto="Mover (1 paso)")
            if not ok: return
            local_ms = int(step.get("wait_ms", 0))
            if local_ms > 0:
                t_end = time.time() + (local_ms/1000.0)
                while time.time() < t_end:
                    time.sleep(0.01)

        elif step_type == "wait":
            ms = int(step.get("wait_ms", 0))
            t_end = time.time() + (ms/1000.0)
            while time.time() < t_end:
                time.sleep(0.01)

        elif step_type == "grip":
            side = step.get("side", "R")
            level = max(0, min(4, int(step.get("level", 1))))
            def _send_grip():
                with serial_lock:
                    puerto_serial.write(f"{{{side}{level}}}".encode('ascii'))
            ok = _send_and_maybe_wait_ack(_send_grip, want_sync, idx+1, f"Pinza {side}{level} (1 paso)")
            if not ok: return
            local_ms = int(step.get("wait_ms", 0))
            if local_ms > 0:
                t_end = time.time() + (local_ms/1000.0)
                while time.time() < t_end:
                    time.sleep(0.01)

        elif step_type == "grips":
            lr = max(0, min(4, int(step.get("levelR", 1))))
            ll = max(0, min(4, int(step.get("levelL", 1))))
            def _send_grips():
                with serial_lock:
                    puerto_serial.write(f"{{R{lr}}}".encode('ascii'))
                    puerto_serial.write(f"{{L{ll}}}".encode('ascii'))
            ok = _send_and_maybe_wait_ack(_send_grips, want_sync, idx+1, f"Pinzas R{lr}/L{ll} (1 paso)")
            if not ok: return
            local_ms = int(step.get("wait_ms", 0))
            if local_ms > 0:
                t_end = time.time() + (local_ms/1000.0)
                while time.time() < t_end:
                    time.sleep(0.01)

        elif step_type == "motor":
            which = step.get("which", 'A')
            sign  = step.get("sign", '+')
            pct   = max(0, min(100, int(step.get("pct", 0))))
            def _send_motor():
                with serial_lock:
                    puerto_serial.write(f"&{which}{sign}{pct:03d}".encode('ascii'))
            ok = _send_and_maybe_wait_ack(_send_motor, want_sync, idx+1, f"Motor {which} (1 paso)")
            if not ok: return
            local_ms = int(step.get("wait_ms", 0))
            if local_ms > 0:
                t_end = time.time() + (local_ms/1000.0)
                while time.time() < t_end:
                    time.sleep(0.01)
            if step.get("autostop", False) and local_ms > 0:
                def _send_stop():
                    with serial_lock:
                        puerto_serial.write(b'&S')
                _send_and_maybe_wait_ack(_send_stop, wait_ack=False, idx=idx+1, contexto="Parada (&S) auto (1 paso)")

        elif step_type == "motors":
            sA = step.get("signA", '+'); pA = max(0, min(100, int(step.get("pctA", 0))))
            sB = step.get("signB", '+'); pB = max(0, min(100, int(step.get("pctB", 0))))
            def _send_motors():
                with serial_lock:
                    puerto_serial.write(f"&A{sA}{pA:03d}".encode('ascii'))
                    puerto_serial.write(f"&B{sB}{pB:03d}".encode('ascii'))
            ok = _send_and_maybe_wait_ack(_send_motors, want_sync, idx+1, "Motores (1 paso)")
            if not ok: return
            local_ms = int(step.get("wait_ms", 0))
            if local_ms > 0:
                t_end = time.time() + (local_ms/1000.0)
                while time.time() < t_end:
                    time.sleep(0.01)
            if step.get("autostop", False) and local_ms > 0:
                def _send_stop():
                    with serial_lock:
                        puerto_serial.write(b'&S')
                _send_and_maybe_wait_ack(_send_stop, wait_ack=False, idx=idx+1, contexto="Parada (&S) auto (1 paso)")

        elif step_type == "motors_stop":
            def _send_stop():
                with serial_lock:
                    puerto_serial.write(b'&S')
            _send_and_maybe_wait_ack(_send_stop, wait_ack=False, idx=idx+1, contexto="Parada (&S) (1 paso)")

        label_estado.config(text=f"Ejecutado paso {idx+1} ({step_type})", fg="blue")
    finally:
        _seq_clear_running()  # <<< NUEVO

def _seq_load_controls_from_selected():
    idx = _get_selected_index()
    if idx is None:
        messagebox.showinfo("Secuencia", "Seleccioná un paso primero.")
        return
    step = secuencia[idx]
    t = step.get("type")

    global updating
    updating = True
    try:
        if t == "move":
            mp = {c:(p,v) for (c,p,v) in step.get("items", [])}
            for canal, pwm_slider, vel_var, activo_var, pwm_box, vel_box, vel_combo in sliders_info:
                if canal in mp:
                    pwm, vel = mp[canal]
                    pwm_slider.set(pwm)
                    pwm_box.config(state='normal'); pwm_box.delete(0, tk.END); pwm_box.insert(0, str(pwm)); pwm_box.config(state='readonly')
                    vel_var.set(f"{vel:02d}")
                    vel_box.config(state='normal'); vel_box.delete(0, tk.END); vel_box.insert(0, f"{vel:02d}"); vel_box.config(state='readonly')
                    vel_combo.set(f"{vel:02d}")
                    activo_var.set(True)
            label_estado.config(text=f"Controles cargados desde paso {idx+1} (move)", fg="blue")

        elif t == "motor":
            try:
                motor_which_var.set(step.get("which","A"))
                motor_sign_var.set(step.get("sign","+"))
                motor_pct_var.set(f"{int(step.get('pct',0)):03d}")
                motors_autostop_var.set(bool(step.get("autostop", False)))
            except Exception:
                pass
            label_estado.config(text=f"Controles cargados desde paso {idx+1} (motor)", fg="blue")

        elif t == "motors":
            try:
                motors_signA_var.set(step.get("signA","+"))
                motors_pctA_var.set(f"{int(step.get('pctA',0)):03d}")
                motors_signB_var.set(step.get("signB","+"))
                motors_pctB_var.set(f"{int(step.get('pctB',0)):03d}")
                motors_autostop_var.set(bool(step.get("autostop", False)))
            except Exception:
                pass
            label_estado.config(text=f"Controles cargados desde paso {idx+1} (motors)", fg="blue")

        else:
            messagebox.showinfo("Secuencia", "Este tipo de paso no tiene controles cargables (p. ej. pinzas o parada).")
    finally:
        updating = False

def _seq_replace_from_controls():
    idx = _get_selected_index()
    if idx is None:
        messagebox.showinfo("Secuencia", "Seleccioná un paso primero.")
        return

    old = secuencia[idx]
    t = old.get("type")

    try:
        wait_ms = int(seq_wait_entry.get().strip()) if seq_wait_entry else 0
    except:
        wait_ms = 0
    sync_flag = bool(seq_sync_var.get())

    if t == "move":
        items = []
        for canal, pwm_slider, vel_var, activo_var, *_ in sliders_info:
            if activo_var.get():
                pwm = int(round(pwm_slider.get()))
                vel = int(vel_var.get())
                items.append((canal, pwm, vel))
        if not items:
            messagebox.showinfo("Secuencia", "No hay canales activos para reemplazar el paso.")
            return
        secuencia[idx] = {
            "type":"move","items":items,"sync":sync_flag,"wait_ms":max(0,wait_ms),"desc":old.get("desc","")
        }

    elif t == "motor":
        try:
            which = motor_which_var.get()
            sign  = motor_sign_var.get()
            pct   = int(motor_pct_var.get())
        except Exception:
            pct = 0
        pct = max(0, min(100, pct))
        if which not in ("A","B"): which = "A"
        if sign not in ("+","-"):  sign  = "+"
        secuencia[idx] = {
            "type":"motor","which":which,"sign":sign,"pct":pct,
            "sync":sync_flag,"wait_ms":max(0,wait_ms),
            "autostop": old.get("autostop", False),
            "desc":old.get("desc","")
        }

    elif t == "motors":
        try:
            sA = motors_signA_var.get(); pA = int(motors_pctA_var.get())
            sB = motors_signB_var.get(); pB = int(motors_pctB_var.get())
        except Exception:
            pA = pB = 0; sA = sB = "+"
        pA = max(0, min(100, pA)); pB = max(0, min(100, pB))
        if sA not in ("+","-"): sA = "+"
        if sB not in ("+","-"): sB = "+"
        secuencia[idx] = {
            "type":"motors","signA":sA,"pctA":pA,"signB":sB,"pctB":pB,
            "sync":sync_flag,"wait_ms":max(0,wait_ms),
            "autostop": old.get("autostop", False),
            "desc":old.get("desc","")
        }

    else:
        messagebox.showinfo("Secuencia", "Por ahora reemplazo soporta tipos move/motor/motors.")
        return

    _seq_refresh_tree()
    try:
        seq_tree.selection_set(f"s{idx+1}")
    except:
        pass
    label_estado.config(text=f"Paso {idx+1} reemplazado desde controles", fg="green")

# ======================= SCROLL CON RUEDA =======================
def _enable_mousewheel_scroll(root_widget, canvas_widget):
    def _on_mousewheel(event):
        if sys.platform.startswith('win') or sys.platform == 'darwin':
            step = -1 * (event.delta // 120)
            canvas_widget.yview_scroll(step, "units")
            return "break"
    root_widget.bind_all("<MouseWheel>", _on_mousewheel)
    root_widget.bind_all("<Button-4>", lambda e: canvas_widget.yview_scroll(-1, "units"))
    root_widget.bind_all("<Button-5>", lambda e: canvas_widget.yview_scroll( 1, "units"))

# ======================= TOGGLES: ASCII y RX =======================
def toggle_ascii():
    global ascii_visible
    if not ascii_frame or not ascii_toggle_btn:
        return
    if ascii_visible:
        ascii_frame.pack_forget()
        ascii_toggle_btn.config(text="Mostrar ASCII")
        ascii_visible = False
    else:
        ascii_frame.pack(fill=tk.BOTH, padx=10, pady=(0,5))
        ascii_toggle_btn.config(text="Ocultar ASCII")
        ascii_visible = True

def toggle_rx():
    global rx_visible
    if not rx_frame or not rx_toggle_btn:
        return
    if rx_visible:
        rx_frame.pack_forget()
        rx_toggle_btn.config(text="Mostrar RX")
        rx_visible = False
    else:
        rx_frame.pack(fill=tk.BOTH, padx=10, pady=(0,10))
        rx_toggle_btn.config(text="Ocultar RX")
        rx_visible = True

# ========== Activar/Desactivar TODOS los checks "Activar" ==========
activos_toggle_estado = True  # True = están activos; el botón mostrará "Desactivar todos"
btn_toggle_activos = None

def toggle_activos_global():
    """Activa/Desactiva todas las casillas "Activar" de los 32 canales."""
    global activos_toggle_estado
    nuevo_valor = 0 if activos_toggle_estado else 1
    for _, _, _, activo_var, *_ in sliders_info:
        try:
            activo_var.set(nuevo_valor)
        except Exception:
            pass
    activos_toggle_estado = not activos_toggle_estado
    if btn_toggle_activos and btn_toggle_activos.winfo_exists():
        btn_toggle_activos.config(text=("Desactivar todos" if activos_toggle_estado else "Activar todos"))

# ======================= INTERFAZ GRÁFICA =======================
def _start_app():
    global combo_puertos, label_estado, lbl_fb, lbl_lr, sliders_info, toggle_pos_btn
    global ascii_send_text, rx_text, rx_vsb, ascii_frame, ascii_toggle_btn, rx_frame, rx_toggle_btn, _root_ref
    global seq_tree, seq_wait_entry, seq_sync_var
    global motor_which_var, motor_sign_var, motor_pct_var
    global motors_signA_var, motors_pctA_var, motors_signB_var, motors_pctB_var
    global motors_autostop_var
    global btn_toggle_activos

    root = tk.Tk()
    _root_ref = root
    root.title("Control de Servos Humanoide")

    # ----- Barra superior -----
    frame_top = tk.Frame(root); frame_top.pack(fill=tk.X, padx=10, pady=5)
    tk.Label(frame_top, text="Puerto:").pack(side=tk.LEFT)
    combo_puertos = ttk.Combobox(frame_top, values=puertos_disponibles(), width=15); combo_puertos.pack(side=tk.LEFT)
    tk.Button(frame_top, text="Refrescar", command=refrescar_puertos).pack(side=tk.LEFT, padx=5)
    tk.Button(frame_top, text="Conectar", command=conectar_serial).pack(side=tk.LEFT, padx=5)
    label_estado = tk.Label(frame_top, text="No conectado", fg="red"); label_estado.pack(side=tk.LEFT, padx=10)

    # ----- Motores DC (BTS7960) -----
    frame_motores = tk.LabelFrame(root, text="Motores DC (BTS7960)"); frame_motores.pack(fill=tk.X, padx=10, pady=5)

    frmA = tk.Frame(frame_motores); frmA.pack(fill=tk.X, pady=2)
    varA = tk.StringVar(value="000")
    tk.Label(frmA, text="Motor A %:").pack(side=tk.LEFT)
    entryA = ttk.Entry(frmA, width=5, textvariable=varA); entryA.pack(side=tk.LEFT)
    tk.Button(frmA, text="A +", command=lambda: motor_cmd('A','+',varA.get())).pack(side=tk.LEFT, padx=3)
    tk.Button(frmA, text="A -", command=lambda: motor_cmd('A','-',varA.get())).pack(side=tk.LEFT, padx=3)
    tk.Label(frmA, text="Dir A:").pack(side=tk.LEFT, padx=(10,2))
    dirA_var = tk.StringVar(value='+')
    ttk.Combobox(frmA, textvariable=dirA_var, width=2, values=['+','-']).pack(side=tk.LEFT)

    frmB = tk.Frame(frame_motores); frmB.pack(fill=tk.X, pady=2)
    varB = tk.StringVar(value="000")
    tk.Label(frmB, text="Motor B %:").pack(side=tk.LEFT)
    entryB = ttk.Entry(frmB, width=5, textvariable=varB); entryB.pack(side=tk.LEFT)
    tk.Button(frmB, text="B +", command=lambda: motor_cmd('B','+',varB.get())).pack(side=tk.LEFT, padx=3)
    tk.Button(frmB, text="B -", command=lambda: motor_cmd('B','-',varB.get())).pack(side=tk.LEFT, padx=3)
    tk.Label(frmB, text="Dir B:").pack(side=tk.LEFT, padx=(10,2))
    dirB_var = tk.StringVar(value='+')
    ttk.Combobox(frmB, textvariable=dirB_var, width=2, values=['+','-']).pack(side=tk.LEFT)

    tk.Button(frame_motores, text="PARADA (&S)", command=motores_parada).pack(side=tk.LEFT, padx=10)
    tk.Button(frame_motores,
              text="ACCIONAR AMBOS (&A &B)",
              command=lambda: motores_aplicar_ambos(dirA_var.get(), dirB_var.get(), varA.get(), varB.get())
             ).pack(side=tk.LEFT, padx=10)
    tk.Button(frame_motores, text="HOME ($)", command=enviar_home).pack(side=tk.LEFT, padx=10)
    tk.Button(frame_motores, text="PARADA servos (|)", command=enviar_parada_servos_servos).pack(side=tk.LEFT, padx=10)
    tk.Button(frame_motores, text="HOME Der (])", command=enviar_home_brazo_derecho).pack(side=tk.LEFT, padx=10)
    tk.Button(frame_motores, text="HOME Izq (%)", command=enviar_home_brazo_izquierdo).pack(side=tk.LEFT, padx=10)
    tk.Button(frame_motores, text="HOME Cabeza (\\)", command=enviar_home_cabeza).pack(side=tk.LEFT, padx=10)

    # ----- Sensores / Cabeza -----
    frame_sens = tk.LabelFrame(root, text="Sensores / Cabeza"); frame_sens.pack(fill=tk.X, padx=10, pady=5)
    tk.Button(frame_sens, text="Medir F/B (_)", command=medir_FB_instantaneo).pack(side=tk.LEFT, padx=5)
    tk.Button(frame_sens, text="Escaneo tilt (^) \n(espera patrón)", command=escaneo_tilt_minFB).pack(side=tk.LEFT, padx=5)
    tk.Button(frame_sens, text="Giro izq ([) \n(espera patrón)", command=giro_izq_medicion_LR).pack(side=tk.LEFT, padx=5)
    lbl_fb = tk.Label(frame_sens, text="F/B: (sin datos)"); lbl_fb.pack(side=tk.LEFT, padx=10)
    lbl_lr = tk.Label(frame_sens, text="L/R: (sin datos)"); lbl_lr.pack(side=tk.LEFT, padx=10)

    # ----- Pinzas (horizontal lado a lado) -----
    frame_pinzas = tk.LabelFrame(root, text="Pinzas"); frame_pinzas.pack(fill=tk.X, padx=10, pady=5)
    pinzas_row = tk.Frame(frame_pinzas); pinzas_row.pack(fill=tk.X)
    colR = tk.Frame(pinzas_row); colR.pack(side=tk.LEFT, padx=(5,20), pady=2)
    tk.Label(colR, text="Pinza Derecha:").pack(side=tk.LEFT)
    tk.Button(colR, text="{R0}", command=lambda: enviar_pinza_derecha(0)).pack(side=tk.LEFT, padx=3)
    tk.Button(colR, text="{R1}", command=lambda: enviar_pinza_derecha(1)).pack(side=tk.LEFT, padx=3)
    tk.Button(colR, text="{R2}", command=lambda: enviar_pinza_derecha(2)).pack(side=tk.LEFT, padx=3)
    tk.Button(colR, text="{R3}", command=lambda: enviar_pinza_derecha(3)).pack(side=tk.LEFT, padx=3)
    tk.Button(colR, text="{R4}", command=lambda: enviar_pinza_derecha(4)).pack(side=tk.LEFT, padx=3)
    sep = tk.Frame(pinzas_row, width=12); sep.pack(side=tk.LEFT)
    colL = tk.Frame(pinzas_row); colL.pack(side=tk.LEFT, padx=(20,5), pady=2)
    tk.Label(colL, text="Pinza Izquierda:").pack(side=tk.LEFT)
    tk.Button(colL, text="{L0}", command=lambda: enviar_pinza_izquierda(0)).pack(side=tk.LEFT, padx=3)
    tk.Button(colL, text="{L1}", command=lambda: enviar_pinza_izquierda(1)).pack(side=tk.LEFT, padx=3)
    tk.Button(colL, text="{L2}", command=lambda: enviar_pinza_izquierda(2)).pack(side=tk.LEFT, padx=3)
    tk.Button(colL, text="{L3}", command=lambda: enviar_pinza_izquierda(3)).pack(side=tk.LEFT, padx=3)
    tk.Button(colL, text="{L4}", command=lambda: enviar_pinza_izquierda(4)).pack(side=tk.LEFT, padx=3)

    # ----- Secuencias -----
    frame_seq = tk.LabelFrame(root, text="Secuencias"); frame_seq.pack(fill=tk.BOTH, padx=10, pady=5)

    # Fila 1 (top): espera + sync + capturar/guardar + pinzas (compacto)
    top_seq = tk.Frame(frame_seq); top_seq.pack(fill=tk.X, pady=2)
    tk.Label(top_seq, text="Espera (ms):").pack(side=tk.LEFT)
    seq_wait_entry = ttk.Entry(top_seq, width=7)
    seq_wait_entry.insert(0, "0")
    seq_wait_entry.pack(side=tk.LEFT, padx=5)
    seq_sync_var = tk.BooleanVar(value=True)
    tk.Checkbutton(top_seq, text="Esperar ACK '*' (excepto motores)", variable=seq_sync_var).pack(side=tk.LEFT, padx=10)
    tk.Button(top_seq, text="Capturar desde sliders", command=_seq_capture_from_sliders).pack(side=tk.LEFT, padx=6)
    tk.Button(top_seq, text="Agregar Espera", command=_seq_add_wait).pack(side=tk.LEFT, padx=6)
    tk.Button(top_seq, text="Guardar (JSON)", command=_seq_save_json).pack(side=tk.LEFT, padx=6)
    tk.Button(top_seq, text="Cargar (JSON)", command=_seq_load_json).pack(side=tk.LEFT, padx=6)

    # Controles pinza (individual y ambas) compactos
    tk.Label(top_seq, text=" | Pinza:").pack(side=tk.LEFT, padx=(8,2))
    side_var = tk.StringVar(value='R')
    ttk.Combobox(top_seq, textvariable=side_var, width=3, values=['R','L'], state='readonly').pack(side=tk.LEFT)
    tk.Label(top_seq, text="Nivel:").pack(side=tk.LEFT, padx=(6,2))
    level_var = tk.StringVar(value='1')
    ttk.Combobox(top_seq, textvariable=level_var, width=3, values=['0','1','2','3','4'], state='readonly').pack(side=tk.LEFT)
    tk.Button(top_seq, text="Agregar Pinza", command=lambda: _seq_add_grip(side_var.get(), level_var.get())).pack(side=tk.LEFT, padx=(6,0))

    tk.Label(top_seq, text=" | Ambas:").pack(side=tk.LEFT, padx=(12,2))
    tk.Label(top_seq, text="R:").pack(side=tk.LEFT)
    level_r_var = tk.StringVar(value='1')
    ttk.Combobox(top_seq, textvariable=level_r_var, width=3, values=['0','1','2','3','4'], state='readonly').pack(side=tk.LEFT)
    tk.Label(top_seq, text="L:").pack(side=tk.LEFT, padx=(8,2))
    level_l_var = tk.StringVar(value='1')
    ttk.Combobox(top_seq, textvariable=level_l_var, width=3, values=['0','1','2','3','4'], state='readonly').pack(side=tk.LEFT)
    tk.Button(top_seq, text="Agregar Ambas", command=lambda: _seq_add_grips(level_r_var.get(), level_l_var.get())).pack(side=tk.LEFT, padx=(6,0))

    # Fila 2: Motores
    motors_seq_row = tk.Frame(frame_seq); motors_seq_row.pack(fill=tk.X, pady=(2,6))
    tk.Label(motors_seq_row, text="Motor:").pack(side=tk.LEFT, padx=(2,2))
    motor_which_var = tk.StringVar(value='A')
    ttk.Combobox(motors_seq_row, textvariable=motor_which_var, width=3, values=['A','B'], state='readonly').pack(side=tk.LEFT)
    motor_sign_var = tk.StringVar(value='+')
    ttk.Combobox(motors_seq_row, textvariable=motor_sign_var, width=2, values=['+','-'], state='readonly').pack(side=tk.LEFT, padx=(4,2))
    tk.Label(motors_seq_row, text="%:").pack(side=tk.LEFT)
    motor_pct_var = tk.StringVar(value='050')
    ttk.Entry(motors_seq_row, width=4, textvariable=motor_pct_var).pack(side=tk.LEFT)
    tk.Button(motors_seq_row, text="Agregar Motor",
              command=lambda: _seq_add_motor(motor_which_var.get(), motor_sign_var.get(), motor_pct_var.get())).pack(side=tk.LEFT, padx=(6,10))

    tk.Label(motors_seq_row, text="Ambos:").pack(side=tk.LEFT, padx=(4,2))
    motors_signA_var = tk.StringVar(value='+'); motors_pctA_var = tk.StringVar(value='050')
    tk.Label(motors_seq_row, text="A").pack(side=tk.LEFT)
    ttk.Combobox(motors_seq_row, textvariable=motors_signA_var, width=2, values=['+','-'], state='readonly').pack(side=tk.LEFT)
    ttk.Entry(motors_seq_row, width=4, textvariable=motors_pctA_var).pack(side=tk.LEFT, padx=(2,6))
    motors_signB_var = tk.StringVar(value='+'); motors_pctB_var = tk.StringVar(value='050')
    tk.Label(motors_seq_row, text="B").pack(side=tk.LEFT)
    ttk.Combobox(motors_seq_row, textvariable=motors_signB_var, width=2, values=['+','-'], state='readonly').pack(side=tk.LEFT)
    ttk.Entry(motors_seq_row, width=4, textvariable=motors_pctB_var).pack(side=tk.LEFT, padx=(2,6))
    tk.Button(motors_seq_row, text="Agregar Ambos",
              command=lambda: _seq_add_motors(motors_signA_var.get(), motors_pctA_var.get(),
                                              motors_signB_var.get(), motors_pctB_var.get())).pack(side=tk.LEFT, padx=(6,10))

    tk.Label(motors_seq_row, text="% giro:").pack(side=tk.LEFT)
    spin_pct_var = tk.StringVar(value='060')
    ttk.Entry(motors_seq_row, width=4, textvariable=spin_pct_var).pack(side=tk.LEFT, padx=(2,6))
    tk.Button(motors_seq_row, text="Giro Derecha",
              command=lambda: _seq_add_motors('-', spin_pct_var.get(), '+', spin_pct_var.get())).pack(side=tk.LEFT)
    tk.Button(motors_seq_row, text="Giro Izquierda",
              command=lambda: _seq_add_motors('+', spin_pct_var.get(), '-', spin_pct_var.get())).pack(side=tk.LEFT, padx=(6,0))

    # checkbox de auto-parada motors
    motors_autostop_var = tk.BooleanVar(value=False)
    tk.Checkbutton(motors_seq_row, text="Auto-parada motores (&S) tras espera", variable=motors_autostop_var)\
      .pack(side=tk.LEFT, padx=(10,0))

    # botón Parada (&S)
    tk.Button(motors_seq_row, text="Parada (&S)", command=_seq_add_motors_stop)\
      .pack(side=tk.LEFT, padx=(8,0))

    # Tabla de pasos
    cols = ("N°", "Tipo", "Detalle", "Sync", "Espera")
    seq_tree_ = ttk.Treeview(frame_seq, columns=cols, show="headings", height=8, selectmode="extended")
    for c in cols:
        seq_tree_.heading(c, text=c)
    seq_tree_.column("N°", width=40, anchor=tk.CENTER)
    seq_tree_.column("Tipo", width=90, anchor=tk.CENTER)
    seq_tree_.column("Detalle", width=220, anchor=tk.W)
    seq_tree_.column("Sync", width=60, anchor=tk.CENTER)
    seq_tree_.column("Espera", width=80, anchor=tk.CENTER)
    seq_tree_.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
    globals()["seq_tree"] = seq_tree_
    seq_tree_.bind("<Double-1>", _seq_on_double_click)
    _seq_refresh_tree()

    # Botones de edición / control
    bot_seq = tk.Frame(frame_seq); bot_seq.pack(fill=tk.X, pady=3)
    tk.Button(bot_seq, text="↑", width=3, command=_seq_move_up).pack(side=tk.LEFT, padx=3)
    tk.Button(bot_seq, text="↓", width=3, command=_seq_move_down).pack(side=tk.LEFT, padx=3)
    tk.Button(bot_seq, text="Copiar", command=_seq_copy).pack(side=tk.LEFT, padx=6)
    tk.Button(bot_seq, text="Pegar", command=_seq_paste).pack(side=tk.LEFT, padx=6)
    tk.Button(bot_seq, text="Duplicar", command=_seq_duplicate).pack(side=tk.LEFT, padx=6)
    tk.Button(bot_seq, text="Borrar", command=_seq_delete).pack(side=tk.LEFT, padx=6)
    tk.Button(bot_seq, text="Limpiar", command=_seq_clear).pack(side=tk.LEFT, padx=6)
    tk.Button(bot_seq, text="Reproducir", command=_seq_play).pack(side=tk.LEFT, padx=15)
    tk.Button(bot_seq, text="Ejecutar paso", command=_seq_run_selected).pack(side=tk.LEFT, padx=6)
    tk.Button(bot_seq, text="Detener", command=_seq_stop).pack(side=tk.LEFT, padx=6)
    tk.Button(bot_seq, text="Paso → RX", command=_seq_dump_selected_to_rx).pack(side=tk.LEFT, padx=6)
    tk.Button(bot_seq, text="Cargar controles", command=_seq_load_controls_from_selected).pack(side=tk.LEFT, padx=6)
    tk.Button(bot_seq, text="Reemplazar paso", command=_seq_replace_from_controls).pack(side=tk.LEFT, padx=6)

    # --- Control por voz (botón + integración mínima con VoiceListener) ---
    voz_activa = tk.BooleanVar(value=False)
    # helper de log a RX
    def _rx_log(line: str):
        global rx_text
        try:
            if rx_text is not None:
                rx_text.configure(state="normal")
                rx_text.insert("end", line + "\n")
                rx_text.see("end")
                rx_text.configure(state="disabled")
        except Exception:
            pass

    try:
        from voz import VoiceListener
    except Exception:
        VoiceListener = None

    _voice_listener = None
    def _on_voice_final(t):
        # Log original text
        try:
            _rx_log("VOZ→ " + str(t))
        except Exception:
            pass

        # Normalizamos texto reconocido
        try:
            txt = str(t or "").strip().casefold()
        except Exception:
            txt = ""


        # --- Wake word obligatorio: 'robot' ---
        # Se requiere que la frase empiece con 'robot ...'
        base_txt = txt
        try:
            import unicodedata
            def _strip_accents(s):
                return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')
            bx = _strip_accents(base_txt)
        except Exception:
            bx = base_txt
        
        bx = bx.strip()
        if bx == 'robot':
            try: _rx_log("VOZ: decime el comando después de 'robot' (p. ej., 'robot saludo').")
            except Exception: pass
            return
        
        if not bx.lower().startswith('robot '):
            try: _rx_log("VOZ: ignorado (debe empezar con 'robot') — oído: " + repr(t))
            except Exception: pass
            return
        
        txt = bx[6:].strip()

                # Comando por voz: limpiar secuencia (sin confirmación cuando es por voz)
        if txt in ("limpiar secuencia", "limpiar la secuencia", "limpiar"):
            def _do_clear_force():
                _rx_log("VOZ: limpiar secuencia (sin confirmación)")
                try:
                    secuencia.clear()
                    _seq_refresh_tree()
                except Exception as e:
                    _rx_log("VOZ: error al limpiar - " + str(e))
            if _root_ref is not None:
                _root_ref.after(0, _do_clear_force)
            else:
                _do_clear_force()
            return

        # Caso probado: 'saludo' → cargar ./secuencias/saludo.json (no ejecutar)
        if txt == "saludo":
            try:
                base_dir = _ensure_sequences_dir()
                fname = os.path.join(base_dir, "saludo.json")
                if os.path.exists(fname):
                    def _do():
                        _seq_load_from_file(fname)
                        # Auto-play tras carga
                        try:
                            _seq_play()
                        except Exception as e:
                            _rx_log('VOZ: error al ejecutar secuencia - ' + str(e))
                    try:
                        if _root_ref is not None:
                            _root_ref.after(0, _do)
                        else:
                            _do()
                    except Exception as e:
                        _rx_log("VOZ: error al cargar saludo.json - " + str(e))
                else:
                    _rx_log("VOZ: no encontré 'secuencias/saludo.json'")
            except Exception as e:
                _rx_log("VOZ: error preparando carga - " + str(e))
            return

        # Cualquier otra palabra: intentar ./secuencias/<txt>.json
        if txt:
            try:
                base_dir = _ensure_sequences_dir()
                fname = os.path.join(base_dir, f"{txt}.json")
                if os.path.exists(fname):
                    def _do2():
                        _seq_load_from_file(fname)
                        # Auto-play tras carga
                        try:
                            _seq_play()
                        except Exception as e:
                            _rx_log('VOZ: error al ejecutar secuencia - ' + str(e))
                    try:
                        if _root_ref is not None:
                            _root_ref.after(0, _do2)
                        else:
                            _do2()
                    except Exception as e:
                        _rx_log("VOZ: error al cargar " + os.path.basename(fname) + " - " + str(e))
                else:
                    _rx_log(f"VOZ: no encontré 'secuencias/{txt}.json'")
            except Exception as e:
                _rx_log("VOZ: error preparando carga - " + str(e))

    def _toggle_voz():
        nonlocal _voice_listener  # estamos en el mismo scope de la función constructora principal
        voz_activa.set(not voz_activa.get())
        active = voz_activa.get()
        try:
            btn_voz.configure(text=("Desactivar voz" if active else "Activar voz"))
        except Exception:
            pass
        # Integración mínima
        if VoiceListener is None:
            _rx_log("VOZ: módulo voz no disponible")
            voz_activa.set(False)
            try: btn_voz.configure(text="Activar voz")
            except Exception: pass
            return
        try:
            if active:
                if _voice_listener is None:
                    _voice_listener = VoiceListener(on_final=_on_voice_final)
                if not _voice_listener.is_running():
                    _voice_listener.start()
                    _rx_log("VOZ: activada")
            else:
                if _voice_listener is not None and _voice_listener.is_running():
                    _voice_listener.stop()
                _rx_log("VOZ: desactivada")
        except FileNotFoundError as e:
            voz_activa.set(False)
            try: btn_voz.configure(text="Activar voz")
            except Exception: pass
            _rx_log("VOZ: error de modelo - " + str(e))
        except Exception as e:
            voz_activa.set(False)
            try: btn_voz.configure(text="Activar voz")
            except Exception: pass
            _rx_log("VOZ: error - " + str(e))

    btn_voz = tk.Button(bot_seq, text="Activar voz", command=_toggle_voz)
    btn_voz.pack(side=tk.LEFT, padx=6)

    # ----- Scroll con dos columnas de sliders -----
    canvas = tk.Canvas(root, height=720)
    scrollbar = tk.Scrollbar(root, orient="vertical", command=canvas.yview)
    outer = tk.Frame(canvas)
    outer.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=outer, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    _enable_mousewheel_scroll(root, canvas)

    left_col = tk.Frame(outer)
    right_col = tk.Frame(outer)
    left_col.grid(row=0, column=0, sticky="nsew", padx=(8,4), pady=4)
    right_col.grid(row=0, column=1, sticky="nsew", padx=(4,8), pady=4)

    def _make_channel_widget(parent, canal):
        frame = tk.Frame(parent, relief=tk.RIDGE, borderwidth=1, padx=5, pady=5)
        tk.Label(frame, text=f"Canal {canal:02d}", width=10).pack(side=tk.LEFT)

        pwm_slider = tk.Scale(frame, from_=PWM_MIN, to=PWM_MAX, orient=tk.HORIZONTAL, length=220, resolution=10, label="PWM (µs)")
        pwm_slider.set(1500); pwm_slider.pack(side=tk.LEFT, padx=5)

        vel_var = tk.StringVar(value="01")
        vel_combo = ttk.Combobox(frame, textvariable=vel_var, width=3, values=[f"{i:02d}" for i in range(1, 16)])
        vel_combo.pack(side=tk.LEFT, padx=5)

        activo_var = tk.BooleanVar(value=True)
        tk.Checkbutton(frame, text="Activar", variable=activo_var).pack(side=tk.LEFT, padx=5)

        pwm_box = ttk.Entry(frame, width=6, state='readonly'); pwm_box.pack(side=tk.LEFT, padx=3)
        pwm_box.config(state='normal'); pwm_box.insert(0, '1500'); pwm_box.config(state='readonly')
        vel_box = ttk.Entry(frame, width=3, state='readonly'); vel_box.pack(side=tk.LEFT, padx=3)
        vel_box.config(state='normal'); vel_box.insert(0, '01'); vel_box.config(state='readonly')

        pwm_slider.config(command=lambda val, c=canal, p=pwm_slider, vv=vel_var, ac=activo_var, pb=pwm_box, vb=vel_box: actualizar_desde_pwm(c, p, vv, ac, pb, vb))
        vel_combo.bind('<<ComboboxSelected>>', lambda e, c=canal, vv=vel_var, vb=vel_box: on_vel_changed(e, c, vv, vb))

        sliders_info.append((canal, pwm_slider, vel_var, activo_var, pwm_box, vel_box, vel_combo))
        return frame

    for canal in range(NUM_CANALES):
        parent = left_col if canal < 16 else right_col
        w = _make_channel_widget(parent, canal)
        w.pack(fill=tk.X, padx=4, pady=3)

    # ----- Botonera inferior -----
    frame_bot = tk.Frame(root); frame_bot.pack(pady=10)
    tk.Button(frame_bot, text="Enviar Todos", command=enviar_todos).pack(side=tk.LEFT, padx=5)
    globals()["toggle_pos_btn"] = tk.Button(frame_bot, text="Mostrar tabla de posiciones", command=toggle_tabla_posiciones)
    toggle_pos_btn.pack(side=tk.LEFT, padx=5)

    # botón Activar/Desactivar TODOS
    globals()["btn_toggle_activos"] = tk.Button(frame_bot, text="Desactivar todos", command=toggle_activos_global)
    btn_toggle_activos.pack(side=tk.LEFT, padx=5)

    # Toggle ASCII
    ascii_toggle_btn = tk.Button(root, text="Ocultar ASCII", command=toggle_ascii)
    ascii_toggle_btn.pack(padx=10, pady=(0,5))
    globals()["ascii_toggle_btn"] = ascii_toggle_btn

    # ASCII/TERM: envío (plegable)
    ascii_frame = tk.LabelFrame(root, text="Enviar ASCII (crudo)")
    ascii_frame.pack(fill=tk.BOTH, padx=10, pady=(0,5))
    globals()["ascii_frame"] = ascii_frame
    globals()["ascii_send_text"] = tk.Text(ascii_frame, height=4, wrap="word")
    ascii_send_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(5,0), pady=5)
    send_btns = tk.Frame(ascii_frame); send_btns.pack(side=tk.RIGHT, fill=tk.Y, padx=5, pady=5)
    tk.Button(send_btns, text="Enviar ASCII", command=ascii_enviar).pack(side=tk.TOP, pady=(0,5))

    # Toggle RX (nuevo)
    rx_toggle_btn = tk.Button(root, text="Ocultar RX", command=toggle_rx)
    rx_toggle_btn.pack(padx=10, pady=(0,5))
    globals()["rx_toggle_btn"] = rx_toggle_btn

    # RX en vivo (plegable)
    rx_frame = tk.LabelFrame(root, text="Recibido desde el microcontrolador (en vivo)")
    rx_frame.pack(fill=tk.BOTH, padx=10, pady=(0,10))
    globals()["rx_frame"] = rx_frame
    globals()["rx_text"] = tk.Text(rx_frame, height=8, wrap="word", state='disabled')
    globals()["rx_vsb"] = ttk.Scrollbar(rx_frame, orient="vertical", command=rx_text.yview)
    rx_text.configure(yscrollcommand=rx_vsb.set)
    rx_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(5,0), pady=5)
    rx_vsb.pack(side=tk.RIGHT, fill=tk.Y, padx=5, pady=5)

    rx_btns = tk.Frame(rx_frame); rx_btns.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=(0,5))
    tk.Button(rx_btns, text="Limpiar", command=recibir_limpiar).pack(side=tk.LEFT, padx=3)

    # Exponer controles de motores para otras funciones
    globals()["motor_which_var"] = motor_which_var
    globals()["motor_sign_var"]  = motor_sign_var
    globals()["motor_pct_var"]   = motor_pct_var
    globals()["motors_signA_var"] = motors_signA_var
    globals()["motors_pctA_var"]  = motors_pctA_var
    globals()["motors_signB_var"] = motors_signB_var
    globals()["motors_pctB_var"]  = motors_pctB_var
    globals()["motors_autostop_var"] = motors_autostop_var

    root.after(500, _check_trigger_facial)
    root.mainloop()

if __name__ == '__main__':
    try:
        _start_app()
    except Exception as e:
        try:
            with open('gui_humanoide_error.log', 'w', encoding='utf-8') as f:
                traceback.print_exc(file=f)
        except:
            pass
        try:
            messagebox.showerror("Error", f"{e}")
        except:
            print(f"Error: {e}", file=sys.stderr)