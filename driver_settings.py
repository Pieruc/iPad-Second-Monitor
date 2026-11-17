"""
Lettura/modifica delle risoluzioni del Virtual Display Driver
(C:\\VirtualDisplayDriver\\vdd_settings.xml) e riavvio del driver.
"""

import ctypes
import re
import shutil
import subprocess
from pathlib import Path

SETTINGS = Path(r"C:\VirtualDisplayDriver\vdd_settings.xml")
RES_RE = re.compile(r"<resolution>\s*<width>(\d+)</width>\s*<height>(\d+)</height>"
                    r"\s*<refresh_rate>(\d+)</refresh_rate>\s*</resolution>", re.S)


def read_resolutions():
    """Elenco [(larghezza, altezza, refresh)] presenti nel file del driver."""
    text = SETTINGS.read_text(encoding="utf-8")
    block = re.search(r"<resolutions>(.*?)</resolutions>", text, re.S)
    if not block:
        return []
    return [(int(w), int(h), int(r)) for w, h, r in RES_RE.findall(block.group(1))]


def write_resolutions(resolutions):
    """Sostituisce l'elenco delle risoluzioni (fa un backup .bak la prima volta)."""
    if not resolutions:
        raise ValueError("serve almeno una risoluzione")
    backup = SETTINGS.with_suffix(".xml.bak")
    if not backup.exists():
        shutil.copy(SETTINGS, backup)
    items = "".join(
        f"\n        <resolution>\n            <width>{w}</width>\n            <height>{h}</height>"
        f"\n            <refresh_rate>{r}</refresh_rate>\n        </resolution>"
        for w, h, r in resolutions)
    text = SETTINGS.read_text(encoding="utf-8")
    new, n = re.subn(r"<resolutions>.*?</resolutions>",
                     lambda _: f"<resolutions>{items}\n    </resolutions>", text, flags=re.S)
    if n != 1:
        raise RuntimeError("sezione <resolutions> non trovata nel file del driver")
    SETTINGS.write_text(new, encoding="utf-8")


def gpu_name():
    m = re.search(r"<friendlyname>(.*?)</friendlyname>", SETTINGS.read_text(encoding="utf-8"))
    return m.group(1) if m else "?"


def device_instance_id():
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-PnpDevice -FriendlyName 'Virtual Display Driver').InstanceId"],
        capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    ids = [line.strip() for line in out.stdout.splitlines() if line.strip()]
    return ids[0] if ids else None


def restart_driver():
    """
    Riavvia il driver per fargli rileggere le impostazioni.
    Windows mostra la richiesta dei permessi di amministratore.
    Restituisce True se la richiesta è stata avviata.
    """
    inst = device_instance_id()
    if not inst:
        return False
    # SW_HIDE = 0: nessuna finestra di console, solo la richiesta UAC
    r = ctypes.windll.shell32.ShellExecuteW(None, "runas", "pnputil.exe",
                                            f'/restart-device "{inst}"', None, 0)
    return r > 32
