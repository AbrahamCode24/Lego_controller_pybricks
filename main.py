import asyncio
import threading
import tempfile
import os
from queue import Queue, Empty
import customtkinter as ctk

from bleak import BleakScanner
from pybricksdev.connections.pybricks import PybricksHubBLE

# ==============================================================================
# 1. CÓDIGO DEL FIRMWARE (Lógica del Robot - Gateway)
# ==============================================================================
HUB_GATEWAY_CODE = """
from pybricks.hubs import PrimeHub
from pybricks.pupdevices import Motor
from pybricks.parameters import Port
from pybricks.tools import wait
import uselect
import usys

hub = PrimeHub()
motorB = Motor(Port.B)
motorF = Motor(Port.F)
motorD = Motor(Port.D)

poll = uselect.poll()
poll.register(usys.stdin, uselect.POLLIN)

hub.display.char('G')

while True:
    if poll.poll(10):
        cmd = usys.stdin.read(1)
        if cmd == 'F':
            motorB.run(-1100)
            motorF.run(1100)
        elif cmd == 'T':
            motorB.run(-1100)
            motorF.run(1100)
        elif cmd == 'B':
            motorB.run(800)
            motorF.run(-800)
        elif cmd == 'L':
            motorD.run_target(500, -25, wait=False)
        elif cmd == 'R':
            motorD.run_target(500, 25, wait=False)
        elif cmd == 'C':
            motorD.run_target(500, 0, wait=True)
            motorD.stop()
        elif cmd == 'S':
            motorB.stop()
            motorF.stop()
        elif cmd == 'X':
            motorB.stop()
            motorF.stop()
            motorD.stop()
            raise SystemExit
    wait(10)
"""
# ==============================================================================
# 2. WORKER BLE (Gestor de Comunicación en Segundo Plano)
# ==============================================================================
# Importar la excepción específica para poder ignorarla
from pybricksdev.connections.pybricks import HubDisconnectError 

class BLEWorker:
    def __init__(self, log_queue: Queue):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._thread_main, daemon=True)
        self.queue = None
        self.hub = None
        self.running = threading.Event()
        self.log_queue = log_queue
        self._target_device = None
        self._connect_request = asyncio.Event()
        self.run_task = None 

        # Diccionario para traducir letras a texto legible en el Log
        self.CMD_DESC = {
            'F': "▲ Avanzando",
            'B': "▼ Retrocediendo",
            'L': "◀ Viraje Izquierda",
            'R': "▶ Viraje Derecha",
            'C': "● Dirección Centrada",
            'T': " ¡TURBO! ",
            'S': "Deteniendo Motores",
            'X': "Desconexión"
        }

    def log(self, msg):
        self.log_queue.put(msg)

    def _thread_main(self):
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self._runner())
        self.loop.run_forever()

    async def _runner(self):
        temp_path = None
        while True:
            await self._connect_request.wait()
            
            try:
                if not self._target_device:
                    self._connect_request.clear()
                    continue

                self.log(f"Conectando a {self._target_device.name}...")
                self.hub = PybricksHubBLE(self._target_device)
                await self.hub.connect()

                self.log("Cargando firmware gateway...")
                with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as tf:
                    tf.write(HUB_GATEWAY_CODE)
                    temp_path = tf.name

                self.queue = asyncio.Queue()
                
                # Guardamos la tarea en self.run_task para poder cancelarla limpiamente luego
                self.run_task = asyncio.create_task(self.hub.run(temp_path))
                
                await asyncio.sleep(1) 
                self.running.set()
                self.log("¡CONEXIÓN ESTABLECIDA!") 

                while self.running.is_set():
                    try:
                        cmd = await self.queue.get()
                        if self.hub: 
                            await self.hub.write(cmd.encode())
                    except asyncio.CancelledError:
                        break 
                    except Exception as e:
                        if "disconnected" in str(e):
                            break
                        self.log(f"Error enviando: {e}")

            except HubDisconnectError:
                # Ignoramos el error de desconexión normal
                pass
            except Exception as e:
                self.log(f"Error general: {e}")
            finally:
                # Limpieza de archivo temporal
                if temp_path and os.path.exists(temp_path):
                    try: os.unlink(temp_path)
                    except: pass
                
                # Cancelar la tarea que corre el programa en el hub si sigue viva
                if self.run_task and not self.run_task.done():
                    self.run_task.cancel()
                    try: await self.run_task
                    except: pass 
                
                if self.hub:
                    try: await self.hub.disconnect()
                    except: pass
                
                self.running.clear()
                self._connect_request.clear()
                self._target_device = None
                self.hub = None
                self.log("Sistema Desconectado.")

    def start(self):
        if not self.thread.is_alive():
            self.thread.start()

    def connect_to_device(self, device):
        self._target_device = device
        self.loop.call_soon_threadsafe(self._connect_request.set)

    def send_command(self, char):
        if self.running.is_set() and self.queue:
            # 1. Enviar el comando al loop asyncio
            self.loop.call_soon_threadsafe(self.queue.put_nowait, char)
            
            # 2. Traducir y mostrar en GUI
            desc = self.CMD_DESC.get(char, f"Comando: {char}")
            # Evitamos spamear el log si es solo soltar tecla (Stop), opcionalmente
            self.log(f"Acción: {desc}")

    def stop_connection(self):
        self.running.clear()
        # Enviamos 'X' para que el Hub se apague solo antes de cortar el Bluetooth
        if self.queue:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, "X")
# ===================================================================
# 3. VENTANA DE SELECCIÓN 
# ===================================================================
class DeviceSelectWindow(ctk.CTkToplevel):
    def __init__(self, parent, callback):
        super().__init__(parent)
        self.callback = callback
        self.title("Buscar HUB LEGO")
        self.geometry("400x400")
        self.attributes("-topmost", True)
        self.grab_set()

        self.lbl = ctk.CTkLabel(self, text="Escaneando dispositivos BLE...", font=("Arial", 14))
        self.lbl.pack(pady=10)

        self.scroll = ctk.CTkScrollableFrame(self)
        self.scroll.pack(expand=True, fill="both", padx=10, pady=10)

        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self):
        loop = asyncio.new_event_loop()
        devices = loop.run_until_complete(BleakScanner.discover(timeout=4.0))
        loop.close()
        self.after(0, lambda: self._show(devices))

    def _show(self, devices):
        self.lbl.configure(text="Seleccione su dispositivo:")
        found = False
        for d in devices:
            if d.name and d.name != "Unknown":
                found = True
                btn = ctk.CTkButton(
                    self.scroll,
                    text=f"{d.name}\n[{d.address}]",
                    command=lambda dev=d: self._select(dev),
                    height=40,
                    fg_color="#1F6AA5"
                )
                btn.pack(pady=5, fill="x")
        
        if not found:
            self.lbl.configure(text="No se encontraron Hubs.")

    def _select(self, device):
        self.callback(device)
        self.destroy()

# ==========================================
# 4. GUI PRINCIPAL 
# ==========================================
class LegoNitroGUI:
    def create_btn(self, t, c, r, col, rel):
        b = ctk.CTkButton(self.cf, text=t, height=45, fg_color=self.COLOR_BTN_NORMAL)

    COLOR_BTN_NORMAL = "#1F6AA5"
    COLOR_BTN_ACTIVE = "#144870"
    COLOR_TURBO_NORMAL = "#D32F2F"
    COLOR_TURBO_ACTIVE = "#8E0000"
    # ---------------------
    def __init__(self, root):
        self.root = root
        self.root.title("Control de Camión Minero - LEGO SPIKE")
        self.root.geometry("600x600")

        # Variables de estado
        self.keys_pressed = set()
        self.log_queue = Queue()
        
        # Iniciar Worker
        self.worker = BLEWorker(self.log_queue)
        self.worker.start()

        # Construir Interfaz
        self._build_ui()
        
        # Iniciar loop de logs
        self._poll_logs()

    def _build_ui(self):
        # --- PANEL SUPERIOR (Conexión) ---
        top_frame = ctk.CTkFrame(self.root)
        top_frame.pack(fill="x", padx=15, pady=15)

        self.btn_connect = ctk.CTkButton(top_frame, text="BUSCAR Y CONECTAR", command=self.open_selector, width=180)
        self.btn_connect.pack(side="left", padx=5, pady=5)

        self.btn_disconnect = ctk.CTkButton(top_frame, text="DESCONECTAR", command=self.on_disconnect, width=120, fg_color="#555555")
        self.btn_disconnect.pack(side="left", padx=5, pady=5)
        self.btn_disconnect.configure(state="disabled")

        self.status_lbl = ctk.CTkLabel(top_frame, text="● DESCONECTADO", text_color="red", font=("Arial", 12, "bold"))
        self.status_lbl.pack(side="right", padx=15)

        # --- PANEL CENTRAL (Controles) ---
        self.control_frame = ctk.CTkFrame(self.root)
        self.control_frame.pack(expand=True, fill="both", padx=15, pady=5)
        
        # Configuración de grilla
        self.control_frame.grid_columnconfigure((0, 1, 2), weight=1)
        self.control_frame.grid_rowconfigure((0, 1, 2, 3, 4), weight=1)

        # Botón TURBO (Rojo)
        self.btn_turbo = ctk.CTkButton(
            self.control_frame, 
            text=" TURBO ", 
            fg_color="#D32F2F", 
            hover_color="#B71C1C",
            height=50,
            font=("Arial", 14, "bold")
        )
        self.btn_turbo.grid(row=0, column=1, pady=(20, 10), sticky="ew")
        # Eventos Press/Release para Turbo
        self.btn_turbo.bind("<ButtonPress-1>", lambda e: self.worker.send_command("T"))
        self.btn_turbo.bind("<ButtonRelease-1>", lambda e: self.worker.send_command("S"))

        # Botones de Dirección
        # Avanzar
        self.btn_avanzar = self.create_momentary_btn("▲ AVANZAR", "F", 1, 1)
        
        # Izquierda / Centro / Derecha
        self.btn_izq = self.create_momentary_btn("◀ IZQ", "L", 2, 0)
        
        # Botón Centro (Es de un solo click, no momentary)
        self.btn_centro = ctk.CTkButton(self.control_frame, text="● CENTRAR", command=lambda: self.worker.send_command("C"), height=40)
        self.btn_centro.grid(row=2, column=1, padx=5, pady=5)
        
        self.btn_der = self.create_momentary_btn("DER ▶", "R", 2, 2)
        
        # Retroceder
        self.btn_retro = self.create_momentary_btn("▼ ATRÁS", "B", 3, 1)

        # --- PANEL INFERIOR (Logs) ---
        log_frame = ctk.CTkFrame(self.root)
        log_frame.pack(fill="x", padx=15, pady=15)
        
        ctk.CTkLabel(log_frame, text="Registro de Sistema:").pack(anchor="w", padx=5)
        self.log_box = ctk.CTkTextbox(log_frame, height=120)
        self.log_box.pack(fill="x", padx=5, pady=5)
        self.log_box.configure(state="disabled")

        # Configurar estado inicial (bloqueado)
        self.set_controls_enabled(False)

        # Bindings de Teclado (Globales)
        self.root.bind("<KeyPress>", self._on_key_press)
        self.root.bind("<KeyRelease>", self._on_key_release)

    def create_momentary_btn(self, text, cmd_char, r, c):
        """Crea un botón que envía comando al presionar y Stop al soltar"""
        btn = ctk.CTkButton(self.control_frame, text=text, height=45)
        btn.grid(row=r, column=c, padx=5, pady=5, sticky="ew")
        
        # Vincular eventos de mouse
        btn.bind("<ButtonPress-1>", lambda e: self.worker.send_command(cmd_char))
        btn.bind("<ButtonRelease-1>", lambda e: self.worker.send_command("S"))
        return btn

    def set_controls_enabled(self, enabled: bool):
        """Habilita o deshabilita los botones de control visuales"""
        state = "normal" if enabled else "disabled"
        
        # Lista de botones a controlar
        control_buttons = [
            self.btn_turbo, self.btn_avanzar, self.btn_retro,
            self.btn_izq, self.btn_der, self.btn_centro
        ]
        
        for btn in control_buttons:
            btn.configure(state=state)
            
        if enabled:
            self.btn_connect.configure(state="disabled")
            self.btn_disconnect.configure(state="normal")
            self.status_lbl.configure(text="● CONECTADO", text_color="green")
        else:
            self.btn_connect.configure(state="normal")
            self.btn_disconnect.configure(state="disabled")
            self.status_lbl.configure(text="● DESCONECTADO", text_color="red")

    def open_selector(self):
        DeviceSelectWindow(self.root, self.on_device_selected)

    def on_device_selected(self, device):
        self.log_to_gui(f"Dispositivo seleccionado: {device.name}")
        self.status_lbl.configure(text="CONECTANDO...", text_color="orange")
        self.worker.connect_to_device(device)

    def on_disconnect(self):
        self.worker.stop_connection()
        self.set_controls_enabled(False)

    # --- Lógica de Teclado ---
    def _on_key_press(self, e):
        # Evitar repetición automática de teclas
        if e.keysym in self.keys_pressed:
            return
        self.keys_pressed.add(e.keysym)

        if not self.worker.running.is_set(): return
        
        # --- NUEVA SECCIÓN VISUAL (Prender luz) ---
        if e.keysym == "Up": self.btn_avanzar.configure(fg_color=self.COLOR_BTN_ACTIVE)
        elif e.keysym == "Down": self.btn_retro.configure(fg_color=self.COLOR_BTN_ACTIVE)
        elif e.keysym == "Left": self.btn_izq.configure(fg_color=self.COLOR_BTN_ACTIVE)
        elif e.keysym == "Right": self.btn_der.configure(fg_color=self.COLOR_BTN_ACTIVE)
        elif e.keysym == "Return": self.btn_turbo.configure(fg_color=self.COLOR_TURBO_ACTIVE)
        elif e.keysym == "space": self.btn_centro.configure(fg_color=self.COLOR_BTN_ACTIVE)
        # ------------------------------------------

        # Lógica de envío al robot (Igual que antes)...
        if e.keysym == "Up": self.worker.send_command("F")
        elif e.keysym == "Down": self.worker.send_command("B")
        elif e.keysym == "Left": self.worker.send_command("L")
        elif e.keysym == "Right": self.worker.send_command("R")
        elif e.keysym == "Return": self.worker.send_command("T")
        elif e.keysym == "space": self.worker.send_command("C")

    def _on_key_release(self, e):
        self.keys_pressed.discard(e.keysym)
        if not self.worker.running.is_set(): return

        # Soltar Acelerador -> Stop Tracción ('S')
        if e.keysym in ("Up", "Down", "Return"):
            self.worker.send_command("S")
        
        # --- NUEVA SECCIÓN VISUAL (Apagar luz) ---
        if e.keysym == "Up": self.btn_avanzar.configure(fg_color=self.COLOR_BTN_NORMAL)
        elif e.keysym == "Down": self.btn_retro.configure(fg_color=self.COLOR_BTN_NORMAL)
        elif e.keysym == "Left": self.btn_izq.configure(fg_color=self.COLOR_BTN_NORMAL)
        elif e.keysym == "Right": self.btn_der.configure(fg_color=self.COLOR_BTN_NORMAL)
        elif e.keysym == "Return": self.btn_turbo.configure(fg_color=self.COLOR_TURBO_NORMAL)
        elif e.keysym == "space": self.btn_centro.configure(fg_color=self.COLOR_BTN_NORMAL)
        # -----------------------------------------


    # --- Lógica de Logs ---
    def log_to_gui(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"> {msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _poll_logs(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_to_gui(msg)
                
                # Detectar mensaje de éxito para habilitar controles
                if "¡CONEXIÓN ESTABLECIDA!" in msg:
                    self.set_controls_enabled(True)
                # Detectar desconexión
                if "Sistema Desconectado" in msg:
                    self.set_controls_enabled(False)
                    
        except Empty:
            pass
        self.root.after(100, self._poll_logs)

if __name__ == "__main__":
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    app = LegoNitroGUI(root)
    root.mainloop()