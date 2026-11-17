"""
Pannello di controllo per iPad Second Screen.

- avvia/ferma il collegamento (cavo USB, Wi-Fi HTTPS, Wi-Fi HTTP)
- accende/spegne lo schermo virtuale e ne cambia la risoluzione
- modifica l'elenco delle risoluzioni del driver e lo riavvia
- mostra il registro del programma

Avvio: doppio clic su pannello.pyw (oppure "Secondo Schermo.bat").
"""

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # finestra nitida + coordinate fisiche
except Exception:
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import display            # noqa: E402
import driver_settings    # noqa: E402

SETTINGS_FILE = HERE / "pannello_settings.json"
IPAD_INFO = HERE / "ipad_info.json"
ICON_PNG = HERE / "ios" / "SecondScreen" / "Assets.xcassets" / "AppIcon.appiconset" / "icon-1024.png"
ICON_ICO = HERE / "icona.ico"
BUILD_URL = "https://github.com/Pieruc/iPad-Second-Monitor/actions/workflows/build-ipad-app.yml"

MODES = {"usb": "Cavo USB (app Secondo Schermo)",
         "wifiapp": "Wi-Fi – app Secondo Schermo",
         "https": "Wi-Fi – Safari (HTTPS)",
         "http": "Wi-Fi – Safari (HTTP, senza certificato)"}
DEFAULTS = {"mode": "usb", "fps": 30, "quality": 70, "scale": "1.0", "cursor_size": 1.0,
            "no_dxcam": False, "draw_cursor": True, "manage_display": True,
            "ipad_ip": "", "ipad_code": ""}


def python_console_exe():
    """python.exe anche se il pannello è avviato con pythonw.exe."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.exists():
            return str(cand)
    return str(exe)


class Panel:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.lines = queue.Queue()
        self.settings = {**DEFAULTS, **self._load_settings()}

        root.title("Secondo Schermo – pannello")
        root.minsize(760, 640)
        self._set_icon()
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Status.TLabel", font=("Segoe UI", 10, "bold"))

        main = ttk.Frame(root, padding=12)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(3, weight=1)

        self.status_var = tk.StringVar()
        ttk.Label(main, textvariable=self.status_var, style="Status.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self._build_connection(main).grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        self._build_display(main).grid(row=1, column=1, sticky="nsew", padx=(6, 0))
        self._build_driver(main).grid(row=2, column=0, columnspan=2, sticky="ew", pady=8)
        self._build_log(main).grid(row=3, column=0, columnspan=2, sticky="nsew")
        self._build_footer(main).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_display()
        self.refresh_driver_list()
        self.root.after(200, self._pump_log)
        self.root.after(2000, self._periodic)
        self._update_status()

    # ------------------------------------------------------------------ UI
    def _build_connection(self, parent):
        f = ttk.LabelFrame(parent, text="Collegamento", padding=10)
        self.mode_var = tk.StringVar(value=self.settings["mode"])
        for key, label in MODES.items():
            ttk.Radiobutton(f, text=label, value=key, variable=self.mode_var).pack(anchor="w")
            if key == "wifiapp":
                # indirizzo e codice mostrati dall'app sull'iPad
                w = ttk.Frame(f)
                w.pack(anchor="w", padx=(22, 0), pady=(0, 2))
                self.ip_var = tk.StringVar(value=self.settings["ipad_ip"])
                self.code_var = tk.StringVar(value=self.settings["ipad_code"])
                ttk.Label(w, text="IP iPad").pack(side="left")
                ttk.Entry(w, textvariable=self.ip_var, width=15).pack(side="left", padx=(4, 10))
                ttk.Label(w, text="Codice").pack(side="left")
                ttk.Entry(w, textvariable=self.code_var, width=8).pack(side="left", padx=4)

        grid = ttk.Frame(f)
        grid.pack(fill="x", pady=(8, 4))
        self.fps_var = tk.IntVar(value=self.settings["fps"])
        self.quality_var = tk.IntVar(value=self.settings["quality"])
        self.scale_var = tk.StringVar(value=self.settings["scale"])
        self.cursor_var = tk.DoubleVar(value=self.settings["cursor_size"])
        rows = [("Immagini al secondo", ttk.Spinbox(grid, from_=5, to=60, increment=5,
                                                     textvariable=self.fps_var, width=7)),
                ("Qualità JPEG", ttk.Spinbox(grid, from_=30, to=95, increment=5,
                                             textvariable=self.quality_var, width=7)),
                ("Scala immagine", ttk.Combobox(grid, values=["1.0", "0.75", "0.5"],
                                                textvariable=self.scale_var, width=6)),
                ("Grandezza puntatore", ttk.Spinbox(grid, from_=0.5, to=3, increment=0.25,
                                                    textvariable=self.cursor_var, width=7))]
        for i, (text, widget) in enumerate(rows):
            ttk.Label(grid, text=text).grid(row=i, column=0, sticky="w", pady=2)
            widget.grid(row=i, column=1, sticky="w", padx=8, pady=2)

        self.draw_cursor_var = tk.BooleanVar(value=self.settings["draw_cursor"])
        self.no_dxcam_var = tk.BooleanVar(value=self.settings["no_dxcam"])
        self.manage_var = tk.BooleanVar(value=self.settings["manage_display"])
        ttk.Checkbutton(f, text="Disegna il puntatore del mouse",
                        variable=self.draw_cursor_var).pack(anchor="w")
        ttk.Checkbutton(f, text="Cattura compatibile (più lenta, senza DXGI)",
                        variable=self.no_dxcam_var).pack(anchor="w")
        ttk.Checkbutton(f, text="Accendi/spegni lo schermo virtuale con il collegamento",
                        variable=self.manage_var).pack(anchor="w")

        b = ttk.Frame(f)
        b.pack(fill="x", pady=(10, 0))
        self.start_btn = ttk.Button(b, text="▶  Avvia", command=self.start)
        self.stop_btn = ttk.Button(b, text="■  Ferma", command=self.stop)
        self.start_btn.pack(side="left")
        self.stop_btn.pack(side="left", padx=6)
        return f

    def _build_display(self, parent):
        f = ttk.LabelFrame(parent, text="Schermo virtuale", padding=10)
        self.display_var = tk.StringVar()
        ttk.Label(f, textvariable=self.display_var, wraplength=330, justify="left").pack(anchor="w")

        b = ttk.Frame(f)
        b.pack(fill="x", pady=8)
        ttk.Button(b, text="Accendi", command=lambda: self._display_action(display.turn_on)).pack(side="left")
        ttk.Button(b, text="Spegni", command=lambda: self._display_action(display.turn_off)).pack(side="left", padx=6)
        ttk.Button(b, text="Aggiorna", command=self.refresh_display).pack(side="left")

        r = ttk.Frame(f)
        r.pack(fill="x", pady=(6, 0))
        ttk.Label(r, text="Risoluzione").pack(side="left")
        self.mode_combo = ttk.Combobox(r, state="readonly", width=12)
        self.mode_combo.pack(side="left", padx=6)
        ttk.Button(r, text="Applica", command=self.apply_resolution).pack(side="left")

        self.ipad_var = tk.StringVar()
        ttk.Label(f, textvariable=self.ipad_var, foreground="#555", wraplength=330,
                  justify="left").pack(anchor="w", pady=(10, 0))
        return f

    def _build_driver(self, parent):
        f = ttk.LabelFrame(parent, text="Risoluzioni disponibili nel driver", padding=10)
        self.res_list = tk.Listbox(f, height=4, exportselection=False)
        self.res_list.grid(row=0, column=0, rowspan=3, sticky="nsew")
        f.columnconfigure(0, weight=1)

        add = ttk.Frame(f)
        add.grid(row=0, column=1, sticky="w", padx=10)
        self.new_w = tk.StringVar(value="2360")
        self.new_h = tk.StringVar(value="1640")
        ttk.Entry(add, textvariable=self.new_w, width=6).pack(side="left")
        ttk.Label(add, text="×").pack(side="left", padx=2)
        ttk.Entry(add, textvariable=self.new_h, width=6).pack(side="left")
        ttk.Button(add, text="Aggiungi", command=self.add_resolution).pack(side="left", padx=6)
        ttk.Button(add, text="Dimensione dell'iPad", command=self.fill_ipad_size).pack(side="left")

        b = ttk.Frame(f)
        b.grid(row=1, column=1, sticky="w", padx=10, pady=4)
        ttk.Button(b, text="Rimuovi selezionata", command=self.remove_resolution).pack(side="left")
        ttk.Button(b, text="Riavvia driver (admin)", command=self.restart_driver).pack(side="left", padx=6)

        self.driver_note = tk.StringVar()
        ttk.Label(f, textvariable=self.driver_note, foreground="#555", wraplength=420,
                  justify="left").grid(row=2, column=1, sticky="w", padx=10)
        return f

    def _build_log(self, parent):
        f = ttk.LabelFrame(parent, text="Registro", padding=6)
        self.log = tk.Text(f, height=10, wrap="word", font=("Consolas", 9), state="disabled",
                           background="#111", foreground="#ddd", insertbackground="#ddd")
        sb = ttk.Scrollbar(f, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return f

    def _build_footer(self, parent):
        f = ttk.Frame(parent)
        ttk.Button(f, text="Apri cartella del programma",
                   command=lambda: os.startfile(HERE)).pack(side="left")
        ttk.Button(f, text="Compilazioni app iPad (GitHub)",
                   command=lambda: webbrowser.open(BUILD_URL)).pack(side="left", padx=6)
        ttk.Button(f, text="Crea collegamento sul desktop",
                   command=self.create_shortcut).pack(side="left")
        return f

    # ------------------------------------------------------------ helpers
    def _set_icon(self):
        try:
            if not ICON_ICO.exists() and ICON_PNG.exists():
                from PIL import Image
                Image.open(ICON_PNG).save(ICON_ICO, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
            if ICON_ICO.exists():
                self.root.iconbitmap(default=str(ICON_ICO))
        except Exception:
            pass

    def _load_settings(self):
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        self.settings.update(mode=self.mode_var.get(), fps=self._int(self.fps_var, 30),
                             quality=self._int(self.quality_var, 70), scale=self.scale_var.get(),
                             cursor_size=self._float(self.cursor_var, 1.0),
                             no_dxcam=self.no_dxcam_var.get(), draw_cursor=self.draw_cursor_var.get(),
                             manage_display=self.manage_var.get(),
                             ipad_ip=self.ip_var.get().strip(), ipad_code=self.code_var.get().strip())
        try:
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _int(var, default):
        try:
            return int(var.get())
        except (tk.TclError, ValueError):
            return default

    @staticmethod
    def _float(var, default):
        try:
            return float(var.get())
        except (tk.TclError, ValueError):
            return default

    def write_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def _update_status(self):
        conn = f"attivo ({MODES[self.mode_var.get()].split(' (')[0]})" if self.running() else "fermo"
        self.status_var.set(f"Collegamento: {conn}")
        self.start_btn.state(["disabled"] if self.running() else ["!disabled"])
        self.stop_btn.state(["!disabled"] if self.running() else ["disabled"])

    # -------------------------------------------------------- collegamento
    def start(self):
        if self.running():
            return
        self._save_settings()
        s = self.settings
        if s["mode"] == "wifiapp" and not (s["ipad_ip"] and s["ipad_code"].isdigit()
                                           and len(s["ipad_code"]) == 6):
            messagebox.showwarning("Wi-Fi", "Inserisci l'IP dell'iPad e il codice a 6 cifre: "
                                            "li mostra l'app Secondo Schermo mentre aspetta il PC.")
            return
        if s["manage_display"] and display.find_virtual() and not display.is_on():
            self.write_log("Accendo lo schermo virtuale…")
            if not display.turn_on():
                self.write_log("[!] non sono riuscito ad accendere lo schermo virtuale")
            self.refresh_display()

        args = [python_console_exe(), "-u", str(HERE / "server.py"), "--keep-display",
                "--fps", str(s["fps"]), "--quality", str(s["quality"]),
                "--scale", str(s["scale"]), "--cursor-size", str(s["cursor_size"])]
        if s["mode"] == "usb":
            args.append("--usb")
        elif s["mode"] == "wifiapp":
            args += ["--wifi-app", s["ipad_ip"], "--code", s["ipad_code"]]
        elif s["mode"] == "http":
            args.append("--http")
        if s["no_dxcam"]:
            args.append("--no-dxcam")
        if not s["draw_cursor"]:
            args.append("--no-cursor")

        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONWARNINGS": "ignore"}
        try:
            self.proc = subprocess.Popen(args, cwd=HERE, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         text=True, encoding="utf-8", errors="replace", env=env,
                                         creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError as e:
            messagebox.showerror("Errore", f"Impossibile avviare il programma:\n{e}")
            return
        self.write_log(f"--- avviato ({MODES[s['mode']]}) ---")
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self._update_status()

    def _reader(self, proc):
        for line in proc.stdout:
            self.lines.put(line)
        self.lines.put(None)  # processo terminato

    def stop(self, turn_off=True):
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        if turn_off and self.manage_var.get() and display.is_on():
            display.turn_off()
            self.write_log("Schermo virtuale spento.")
        self.refresh_display()
        self._update_status()

    def _pump_log(self):
        try:
            while True:
                line = self.lines.get_nowait()
                if line is None:
                    self.write_log("--- programma terminato ---")
                    self._update_status()
                elif line.strip() and "Ctrl+C" not in line:  # nel pannello si usa «Ferma»
                    self.write_log(line)
        except queue.Empty:
            pass
        self.root.after(200, self._pump_log)

    def _periodic(self):
        self.refresh_display(quiet=True)
        self._update_status()
        self.root.after(3000, self._periodic)

    # ---------------------------------------------------- schermo virtuale
    def refresh_display(self, quiet=False):
        dd = display.find_virtual()
        if dd is None:
            self.display_var.set("Virtual Display Driver non trovato.")
            self.mode_combo["values"] = []
            return
        if display.is_on():
            dm = display._current_mode(dd.DeviceName)
            cur = f"{dm.dmPelsWidth}x{dm.dmPelsHeight}"
            self.display_var.set(f"Acceso – {cur} @ ({dm.dmPositionX}, {dm.dmPositionY})\n"
                                 f"Scheda video: {driver_settings.gpu_name()}")
        else:
            cur = None
            self.display_var.set(f"Spento\nScheda video: {driver_settings.gpu_name()}")
        modes = [f"{w}x{h}" for w, h in display.available_modes()]
        self.mode_combo["values"] = modes
        if not self.mode_combo.get() or self.mode_combo.get() not in modes:
            self.mode_combo.set(cur if cur in modes else (modes[0] if modes else ""))

        try:
            info = json.loads(IPAD_INFO.read_text(encoding="utf-8"))
            self.ipad_var.set(f"Ultima finestra iPad rilevata: {info.get('w')}x{info.get('h')}")
        except (OSError, ValueError):
            self.ipad_var.set("")

    def _display_action(self, fn):
        ok = fn()
        self.write_log("Fatto." if ok else "[!] Operazione non riuscita.")
        self.refresh_display()

    def apply_resolution(self):
        value = self.mode_combo.get()
        if "x" not in value:
            return
        w, h = map(int, value.split("x"))
        ok = display.set_resolution(w, h)
        self.write_log(f"Risoluzione {w}x{h}: {'impostata' if ok else 'ERRORE'}")
        self.refresh_display()

    # -------------------------------------------------- risoluzioni driver
    def refresh_driver_list(self):
        self.res_list.delete(0, "end")
        try:
            self.resolutions = driver_settings.read_resolutions()
        except OSError as e:
            self.resolutions = []
            self.driver_note.set(f"Impossibile leggere le impostazioni del driver: {e}")
            return
        for w, h, r in self.resolutions:
            self.res_list.insert("end", f"{w} × {h}   ({r} Hz)")
        loaded = {(w, h) for w, h in display.available_modes()}
        pending = {(w, h) for w, h, _ in self.resolutions} != loaded
        self.driver_note.set("Modifiche non ancora attive: premi «Riavvia driver»." if pending
                             else "Il driver usa già queste risoluzioni.")

    def add_resolution(self):
        try:
            w, h = int(self.new_w.get()), int(self.new_h.get())
            assert 320 <= w <= 7680 and 240 <= h <= 4320
        except (ValueError, AssertionError):
            messagebox.showwarning("Risoluzione", "Inserisci larghezza e altezza valide (es. 2360 × 1640).")
            return
        if any((w, h) == (a, b) for a, b, _ in self.resolutions):
            return
        driver_settings.write_resolutions(self.resolutions + [(w, h, 60)])
        self.write_log(f"Aggiunta {w}x{h} al driver.")
        self.refresh_driver_list()

    def remove_resolution(self):
        sel = self.res_list.curselection()
        if not sel:
            return
        if len(self.resolutions) == 1:
            messagebox.showwarning("Risoluzione", "Deve restare almeno una risoluzione.")
            return
        w, h, _ = self.resolutions[sel[0]]
        driver_settings.write_resolutions([r for i, r in enumerate(self.resolutions) if i != sel[0]])
        self.write_log(f"Rimossa {w}x{h} dal driver.")
        self.refresh_driver_list()

    def fill_ipad_size(self):
        try:
            info = json.loads(IPAD_INFO.read_text(encoding="utf-8"))
            self.new_w.set(str(info["w"]))
            self.new_h.set(str(info["h"]))
        except (OSError, ValueError, KeyError):
            self.new_w.set("2360")
            self.new_h.set("1640")

    def restart_driver(self):
        if self.running() and not messagebox.askyesno(
                "Riavvia driver", "Il collegamento è attivo: l'immagine si interromperà per qualche "
                                  "secondo. Continuare?"):
            return
        if driver_settings.restart_driver():
            self.write_log("Riavvio del driver richiesto (conferma la richiesta di Windows)…")
            self.root.after(6000, lambda: (self.refresh_display(), self.refresh_driver_list()))
        else:
            self.write_log("[!] Riavvio del driver non avviato.")

    # ------------------------------------------------------------- varie
    def create_shortcut(self):
        target = Path(sys.executable).with_name("pythonw.exe")
        if not target.exists():
            target = Path(sys.executable)
        desktop = Path(os.path.expandvars(r"%USERPROFILE%\Desktop"))
        ps = (
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);"
            "$s.TargetPath=$env:TGT;$s.Arguments=$env:ARGS;$s.WorkingDirectory=$env:WD;"
            "$s.IconLocation=$env:ICO;$s.Description='Secondo Schermo';$s.Save();"
            "Write-Output $env:LNK"
        )
        # il Desktop può essere dentro OneDrive: lo chiediamo a Windows
        desk = subprocess.run(["powershell", "-NoProfile", "-Command",
                               "[Environment]::GetFolderPath('Desktop')"],
                              capture_output=True, text=True,
                              creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip() or str(desktop)
        env = {**os.environ, "LNK": str(Path(desk) / "Secondo Schermo.lnk"), "TGT": str(target),
               "ARGS": f'"{HERE / "pannello.pyw"}"', "WD": str(HERE),
               "ICO": str(ICON_ICO) if ICON_ICO.exists() else ""}
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env,
                           capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode == 0:
            self.write_log(f"Collegamento creato: {r.stdout.strip()}")
        else:
            self.write_log(f"[!] Collegamento non creato: {r.stderr.strip()[:200]}")

    def on_close(self):
        self._save_settings()
        if self.running():
            self.stop(turn_off=True)
        self.root.destroy()


def main():
    root = tk.Tk()
    Panel(root)
    root.mainloop()


if __name__ == "__main__":
    main()
