import asyncio
import threading
import tempfile
import os
from queue import Queue, Empty
import customtkinter as ctk

from bleak import BleakScanner 
from pybricksdev.ble import find_device  # type: ignore
from pybricksdev.connections.pybricks import PybricksHubBLE  # type: ignore

# -------------------- Programa enviado al Hub -------------------- 

def create_program(drive_cmd: str) -> str:
    # Definición de comandos y velocidades
    # Nota: 1100 es aprox la velocidad máxima de los motores Spike
    drive_commands = {
        'run_forward': "motorB.run(-800)\nmotorF.run(800)",
        'run_turbo':   "motorB.run(-1100)\nmotorF.run(1100)",
        'run_backward': "motorB.run(500)\nmotorF.run(-500)",
        'stop': "motorB.stop()\nmotorF.stop()\nmotorD.stop()",
        'izquierda': "motorD.run_target(300, -35)\nmotorD.stop()",
        'derecha': "motorD.run_target(300, 35)\nmotorD.stop()",
        'centro': "motorD.run_target(300, 0)\nmotorD.stop()",
    }

    drive_code = drive_commands.get(drive_cmd, "motorB.stop()\nmotorF.stop()\nmotorD.stop()")

    # Tiempos de espera según el comando
    wait_code = ""
    if drive_cmd == 'run_forward':
        wait_code = "wait(600)"
    elif drive_cmd == 'run_turbo':
        wait_code = "wait(2000)"
    elif drive_cmd == 'run_backward':
        wait_code = "wait(800)"
    elif drive_cmd in ['izquierda', 'derecha', 'centro']:
        wait_code = "wait(500)"

    program = f"""
from pybricks.hubs import PrimeHub
from pybricks.pupdevices import Motor
from pybricks.parameters import Port
from pybricks.tools import wait

hub = PrimeHub()
motorB = Motor(Port.B)
motorF = Motor(Port.F)
motorD = Motor(Port.D)

{drive_code}
{wait_code}
motorB.stop()
motorF.stop()
"""
    return program

async def execute_command(hub: PybricksHubBLE, drive_cmd: str, log_cb=None):
    program = create_program(drive_cmd)
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as tf:
        tf.write(program)
        temp_path = tf.name

    try:
        await hub.run(temp_path, wait=True, print_output=False) 
        if log_cb:
            log_cb(f"Ejecutado: {drive_cmd}")
    except Exception as e:
        if log_cb:
            log_cb(f"Error ejecutando comando: {e}")
    finally:
        try:
            os.unlink(temp_path)
        except:
            pass

# -------------------- Ventana de Selección de Dispositivo --------------------

class DeviceSelectWindow(ctk.CTkToplevel):
    def __init__(self, parent, on_select_callback):
        super().__init__(parent)
        self.on_select_callback = on_select_callback
        self.title("Seleccionar Dispositivo")
        self.geometry("400x500")
        self.resizable(False, False)
        
        self.transient(parent)
        self.grab_set()

        self.label = ctk.CTkLabel(self, text="Escaneando dispositivos...", font=("Arial", 16, "bold"))
        self.label.pack(pady=10)

        self.scroll_frame = ctk.CTkScrollableFrame(self, width=350, height=350)
        self.scroll_frame.pack(pady=10, padx=10, fill="both", expand=True)

        self.btn_refresh = ctk.CTkButton(self, text="Escanear de nuevo", command=self.start_scan)
        self.btn_refresh.pack(pady=10)

        self.start_scan()

    def start_scan(self):
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()
        
        self.label.configure(text="Buscando dispositivos...")
        self.btn_refresh.configure(state="disabled")
        threading.Thread(target=self._scan_thread, daemon=True).start()

    def _scan_thread(self):
        async def get_devices():
            return await BleakScanner.discover(timeout=5.0)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        devices = loop.run_until_complete(get_devices())
        loop.close()

        self.after(0, lambda: self._update_list(devices))

    def _update_list(self, devices):
        self.label.configure(text="Selecciona tu Hub:")
        self.btn_refresh.configure(state="normal")
        
        found_any = False
        for dev in devices:
            name = dev.name if dev.name else "Desconocido"
            address = dev.address
            
            if name != "Desconocido": 
                found_any = True
                btn = ctk.CTkButton(
                    self.scroll_frame, 
                    text=f"{name}\n({address})", 
                    command=lambda d=dev: self._on_device_click(d),
                    height=50,
                    anchor="w"
                )
                btn.pack(pady=5, padx=5, fill="x")

        if not found_any:
            lbl = ctk.CTkLabel(self.scroll_frame, text="No se encontraron dispositivos con nombre.")
            lbl.pack(pady=20)

    def _on_device_click(self, device):
        self.on_select_callback(device)
        self.destroy()

# -------------------- Worker BLE -------------------- 

class BLEWorker:
    def __init__(self, log_queue: Queue):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._thread_main, daemon=True)
        self.queue = asyncio.Queue()
        self.hub = None
        self.running = threading.Event()
        self.log_queue = log_queue
        
        self._connect_request = asyncio.Event()
        self._target_device = None

    def log(self, msg: str):
        self.log_queue.put(msg)

    def _thread_main(self):
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self._runner())
        self.loop.run_forever()

    async def _runner(self):
        try:
            while True:
                self.log("Esperando selección de dispositivo...")
                await self._connect_request.wait()
                
                device = self._target_device
                if not device:
                    self._connect_request.clear()
                    continue

                try:
                    self.log(f"Conectando a {device.name}...")
                    self.hub = PybricksHubBLE(device)
                    await self.hub.connect()
                    self.log("Conectado. Listo para conducir.")
                    self.running.set()

                    while self.running.is_set():
                        drive_cmd = await self.queue.get()
                        await execute_command(self.hub, drive_cmd, self.log)
                
                except Exception as e:
                    self.log(f"Error de conexión/ejecución: {e}")
                finally:
                    if self.hub:
                        try:
                            await self.hub.disconnect()
                            self.log("Hub desconectado.")
                        except:
                            pass
                    self.hub = None
                    self.running.clear()
                    self._connect_request.clear()
                    self._target_device = None

        except asyncio.CancelledError:
            pass

    def start(self):
        if not self.thread.is_alive():
            self.thread.start()

    def connect_to_device(self, device):
        if self.loop.is_running():
            async def trigger():
                self._target_device = device
                self._connect_request.set()
            asyncio.run_coroutine_threadsafe(trigger(), self.loop)

    def stop(self):
        if self.loop.is_running():
            for task in asyncio.all_tasks(self.loop):
                task.cancel()
            self.loop.call_soon_threadsafe(self.loop.stop)

    def send_command(self, cmd: str):
        if self.loop.is_running() and self.queue is not None:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, cmd)
    
    def disconnect_now(self):
        self.running.clear()

# -------------------- GUI -------------------- 

class LegoGUI:
    def __init__(self, root: ctk.CTk):
        self.root = root
        self.root.title("Control de Auto LEGO - Pybricks")
        self.root.geometry("600x520") # Aumenté un poco el alto para que quepa el botón extra

        self.log_queue = Queue()
        self.worker = BLEWorker(self.log_queue)
        self.worker.start()
        
        self.simulated_disconnect = False

        self._build_ui()
        self._poll_logs()

    def _build_ui(self):
        top = ctk.CTkFrame(self.root)
        top.pack(fill='x', padx=10, pady=10)
        
        self.btn_connect = ctk.CTkButton(top, text="Buscar y Conectar", command=self.on_connect_click, width=150, height=35)
        self.btn_connect.pack(side='left', padx=10, pady = 10)
        
        self.btn_disconnect = ctk.CTkButton(top, text="Desconectar", command=self.on_disconnect, width=100, height=35)
        self.btn_disconnect.pack(side='left', padx=10)
        
        self.btn_disconnect.configure(state="disabled")

        self.status = ctk.CTkLabel(top, text="Estado: sin conexión")
        self.status.pack(side='right', padx = 10)

        body = ctk.CTkFrame(self.root)
        body.pack(expand=True, padx= 10, pady= 10)

        # --- BOTONES DE MOVIMIENTO ---

        # Botón TURBO (Fila 0) - Rojo y llamativo
        self.btn_turbo = ctk.CTkButton(
            body, 
            text=" TURBO ", 
            command=self.cmd_turbo_click, 
            width=120, 
            height=40,
            fg_color="#D32F2F",     # Rojo oscuro
            hover_color="#B71C1C",  # Rojo más oscuro al pasar el mouse
            font=("Arial", 14, "bold")
        )
        self.btn_turbo.grid(row=0, column=1, pady=(10, 5))

        # Botón Avanzar (Fila 1)
        self.btn_avanzar = ctk.CTkButton(body, text="Avanzar", command=self.cmd_avanzar_click, width= 100, height= 35)
        self.btn_avanzar.grid(row=1, column=1, pady=5)

        # Izquierda / Derecha (Fila 2)
        self.btn_izquierda = ctk.CTkButton(body, text="Izquierda", command=self.cmd_izquierda, width= 100, height= 35)
        self.btn_izquierda.grid(row=2, column=0, padx=10, pady=5)

        self.btn_derecha = ctk.CTkButton(body, text="Derecha", command=self.cmd_derecha, width= 100, height= 35)
        self.btn_derecha.grid(row=2, column=2, padx=10, pady=5)
        
        # Botón Retroceder (Fila 3)
        self.btn_retro = ctk.CTkButton(body, text="Retroceder", command=self.cmd_retro_click, width= 100, height= 35)
        self.btn_retro.grid(row=3, column=1, pady=5)

        # Restablecer (Fila 4)
        self.btn_restablecer = ctk.CTkButton(body, text="Restablecer Dirección", command=self.cmd_restablecer, height = 35)
        self.btn_restablecer.grid(row=4, column=1, pady=15)
        
        self.set_controls_enabled(False)

        # Log Area
        logf = ctk.CTkFrame(self.root)
        logf.pack(fill='both', expand=True, padx=10, pady=10)

        log_title = ctk.CTkLabel(logf, text="Registro")
        log_title.pack(anchor='w', padx=10, pady=(0, 5))

        self.log_text = ctk.CTkTextbox(logf, height=140)
        self.log_text.pack(fill='both', expand=True)
        self.log_text.configure(state='disabled')

    # -------------------- Controles -------------------- 

    def set_controls_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        # Incluimos el botón turbo en la lista de bloqueo/desbloqueo
        buttons = [
            self.btn_turbo, 
            self.btn_avanzar, 
            self.btn_retro, 
            self.btn_izquierda, 
            self.btn_derecha, 
            self.btn_restablecer
        ]
        for btn in buttons:
            btn.configure(state=state)

    def cmd_turbo_click(self):
        if self.worker.running.is_set():
            self.worker.send_command("run_turbo")

    def cmd_avanzar_click(self):
        if self.worker.running.is_set():
            self.worker.send_command("run_forward")

    def cmd_retro_click(self):
        if self.worker.running.is_set():
            self.worker.send_command("run_backward")

    def cmd_izquierda(self):
        if self.worker.running.is_set():
            self.worker.send_command("izquierda")

    def cmd_derecha(self):
        if self.worker.running.is_set():
            self.worker.send_command("derecha")

    def cmd_restablecer(self):
        if self.worker.running.is_set():
            self.worker.send_command("centro")

    # -------------------- Conexión -------------------- 

    def on_connect_click(self):
        DeviceSelectWindow(self.root, self.on_device_selected)

    def on_device_selected(self, device):
        self._log(f"Seleccionado: {device.name} ({device.address})")
        self.status.configure(text="Estado: conectando...")
        
        self.btn_connect.configure(state="disabled")
        self.worker.connect_to_device(device)
        self.wait_connection_ready()

    def wait_connection_ready(self):
        if self.worker.running.is_set():
            self.status.configure(text="Estado: conectado")
            self.set_controls_enabled(True)
            self.btn_connect.configure(state="disabled")
            self.btn_disconnect.configure(state="normal")
        else:
            self.root.after(200, self.wait_connection_ready)

    def on_disconnect(self):
        self.set_controls_enabled(False)
        self.worker.disconnect_now()
        self.status.configure(text="Estado: desconectado")
        self._log("Comando desconexión enviado.")
        
        self.btn_connect.configure(state="normal")
        self.btn_disconnect.configure(state="disabled")

    # -------------------- Logs -------------------- 

    def _log(self, msg: str):
        self.log_queue.put(msg)

    def _poll_logs(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state='normal')
                self.log_text.insert('end', msg + "\n")
                self.log_text.see('end')
                self.log_text.configure(state='disabled')
        except Empty:
            pass
        self.root.after(150, self._poll_logs)

def main():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    app = LegoGUI(root)
    root.resizable(False, False)
    root.mainloop()

if __name__ == '__main__':
    main()