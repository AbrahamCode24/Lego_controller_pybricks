import asyncio
import threading
import tempfile
import os
from queue import Queue, Empty
import tkinter as tk
from tkinter import ttk

from pybricksdev.ble import find_device  # type: ignore
from pybricksdev.connections.pybricks import PybricksHubBLE  # type: ignore

# -------------------- Programa enviado al Hub -------------------- 

def create_program(drive_cmd: str) -> str:
    drive_commands = {
        'run_forward': "motorB.run(-800)\nmotorF.run(800)",
        'run_backward': "motorB.run(400)\nmotorF.run(-400)",
        'stop': "motorB.stop()\nmotorF.stop()\nmotorD.stop()",
        'izquierda': "motorD.run_target(300, -40)\nmotorD.stop()",
        'derecha': "motorD.run_target(300, 40)\nmotorD.stop()",
        'centro': "motorD.run_target(300, 0)\nmotorD.stop()",
    }

    drive_code = drive_commands.get(drive_cmd, "motorB.stop()\nmotorF.stop()\nmotorD.stop()")

    wait_code = ""
    if drive_cmd == 'run_forward':
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

# -------------------- Worker BLE -------------------- 

class BLEWorker:
    def __init__(self, log_queue: Queue):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._thread_main, daemon=True)
        self.queue = asyncio.Queue()  # Cola lista desde el inicio
        self.hub = None
        self.running = threading.Event()
        self.log_queue = log_queue

    def log(self, msg: str):
        self.log_queue.put(msg)

    def _thread_main(self):
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self._runner())
        self.loop.run_forever()

    async def _runner(self):
        try:
            self.log("Buscando hub Bluetooth…")
            device = await find_device()
            if not device:
                self.log("No se encontró hub.")
                return

            self.hub = PybricksHubBLE(device)
            await self.hub.connect()
            self.log("Conectado. Listo para conducir.")
            self.running.set()

            while True:
                drive_cmd = await self.queue.get()
                await execute_command(
                    self.hub,
                    drive_cmd,
                    self.log
                )

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.log(f"Error en worker: {e}")
        finally:
            if self.hub:
                try:
                    await self.hub.disconnect()
                    self.log("Hub desconectado.")
                except Exception as e:
                    self.log(f"Error al desconectar: {e}")
            self.running.clear()

    def start(self):
        if not self.thread.is_alive():
            self.thread.start()

    def stop(self):
        if self.loop.is_running():
            for task in asyncio.all_tasks(self.loop):
                task.cancel()
            self.loop.call_soon_threadsafe(self.loop.stop)

    def send_command(self, cmd: str):
        if self.loop.is_running() and self.queue is not None:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, cmd)

# -------------------- GUI -------------------- 

class LegoGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Control de Auto LEGO – Pybricks")
        self.root.geometry("400x400")

        self.log_queue = Queue()
        self.worker = BLEWorker(self.log_queue)
        self.simulated_disconnect = False  # Flag para desconexión simulada

        self._build_ui()
        self._poll_logs()

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill='x')

        ttk.Button(top, text="Conectar", command=self.on_connect).pack(side='left', padx=5)
        ttk.Button(top, text="Desconectar", command=self.on_disconnect).pack(side='left', padx=5)

        self.status = ttk.Label(top, text="Estado: sin conexión")
        self.status.pack(side='right')

        body = ttk.Frame(self.root, padding=20)
        body.pack(fill='both', expand=True)

        # Botones de movimiento
        self.btn_avanzar = ttk.Button(body, text="Avanzar", command=self.cmd_avanzar_click)
        self.btn_avanzar.grid(row=0, column=1, pady=10)

        self.btn_retro = ttk.Button(body, text="Retroceder", command=self.cmd_retro_click)
        self.btn_retro.grid(row=2, column=1, pady=10)

        self.btn_izquierda = ttk.Button(body, text="Izquierda", command=self.cmd_izquierda)
        self.btn_izquierda.grid(row=1, column=0, padx=10)
        self.btn_derecha = ttk.Button(body, text="Derecha", command=self.cmd_derecha)
        self.btn_derecha.grid(row=1, column=2, padx=10)
        self.btn_restablecer = ttk.Button(body, text="Restablecer Dirección", command=self.cmd_restablecer)
        self.btn_restablecer.grid(row=3, column=1, pady=15)

        # Log
        logf = ttk.Labelframe(self.root, text="Registro")
        logf.pack(fill='both', expand=True, padx=10, pady=10)
        self.log_text = tk.Text(logf, height=7, wrap='word')
        self.log_text.pack(fill='both', expand=True)
        self.log_text.configure(state='disabled')

    # -------------------- Controles -------------------- 

    def set_controls_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for btn in [self.btn_avanzar, self.btn_retro, self.btn_izquierda, self.btn_derecha, self.btn_restablecer]:
            btn.config(state=state)

    def cmd_avanzar_click(self):
        if self.worker.running.is_set() and not self.simulated_disconnect:
            self.worker.send_command("run_forward")

    def cmd_retro_click(self):
        if self.worker.running.is_set() and not self.simulated_disconnect:
            self.worker.send_command("run_backward")

    def cmd_izquierda(self):
        if self.worker.running.is_set() and not self.simulated_disconnect:
            self.worker.send_command("izquierda")

    def cmd_derecha(self):
        if self.worker.running.is_set() and not self.simulated_disconnect:
            self.worker.send_command("derecha")

    def cmd_restablecer(self):
        if self.worker.running.is_set() and not self.simulated_disconnect:
            self.worker.send_command("centro")

    # -------------------- Conexión -------------------- 

    def on_connect(self):
        if self.simulated_disconnect:
            # Reconexión simulada: desbloquea los botones
            self.set_controls_enabled(True)
            self.simulated_disconnect = False
            self.status.configure(text="Estado: Conectado")
            self._log("Hub conectado.")
        else:
            # Primera conexión real
            self.status.configure(text="Estado: conectando…")
            self.worker.start()

            def wait_ready():
                if self.worker.running.is_set():
                    self.status.configure(text="Estado: conectado")
                else:
                    self.root.after(200, wait_ready)

            wait_ready()

    def on_disconnect(self):
        # Desconexión simulada: bloquea los controles
        self.set_controls_enabled(False)
        self.simulated_disconnect = True
        self.status.configure(text="Estado: desconectando")
        self._log("Hub desconectado")

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
    root = tk.Tk()
    app = LegoGUI(root)
    root.mainloop()

if __name__ == '__main__':
    main()
