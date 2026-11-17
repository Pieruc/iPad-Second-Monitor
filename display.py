"""
Accende/spegne lo schermo virtuale (Virtual Display Driver) senza permessi
di amministratore, staccandolo dal desktop come fa Impostazioni → Schermo.

Uso da riga di comando:
    python display.py status
    python display.py on
    python display.py off
"""

import ctypes
import json
import time
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.windll.user32

STATE_FILE = Path(__file__).parent / "display_state.json"
DEFAULT_MODE = {"width": 1920, "height": 1080, "x": None, "y": 0}

DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x1
DISPLAY_DEVICE_PRIMARY_DEVICE = 0x4
DM_POSITION = 0x20
DM_PELSWIDTH = 0x80000
DM_PELSHEIGHT = 0x100000
CDS_UPDATEREGISTRY = 0x1
CDS_NORESET = 0x10000000
ENUM_CURRENT_SETTINGS = -1
DISP_CHANGE_SUCCESSFUL = 0


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]


class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32),
        ("dmSpecVersion", wintypes.WORD), ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG),
        ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
        ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short), ("dmFormName", wintypes.WCHAR * 32),
        ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD), ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD),
    ]


assert ctypes.sizeof(DEVMODEW) == 220


def _devices():
    i = 0
    while True:
        dd = DISPLAY_DEVICEW(cb=ctypes.sizeof(DISPLAY_DEVICEW))
        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
            return
        yield dd
        i += 1


def find_virtual():
    """Restituisce il DISPLAY_DEVICE dello schermo virtuale, o None."""
    for dd in _devices():
        if "virtual display" in dd.DeviceString.lower():
            return dd
    return None


def _primary():
    for dd in _devices():
        if dd.StateFlags & DISPLAY_DEVICE_PRIMARY_DEVICE:
            return dd
    return None


def _current_mode(name):
    dm = DEVMODEW(dmSize=ctypes.sizeof(DEVMODEW))
    if user32.EnumDisplaySettingsW(name, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
        return dm
    return None


def is_on():
    dd = find_virtual()
    return bool(dd and dd.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP)


def _apply(name, width, height, x, y):
    dm = DEVMODEW(dmSize=ctypes.sizeof(DEVMODEW))
    dm.dmFields = DM_POSITION | DM_PELSWIDTH | DM_PELSHEIGHT
    dm.dmPelsWidth, dm.dmPelsHeight = width, height
    dm.dmPositionX, dm.dmPositionY = x, y
    r = user32.ChangeDisplaySettingsExW(name, ctypes.byref(dm), None,
                                        CDS_UPDATEREGISTRY | CDS_NORESET, None)
    if r != DISP_CHANGE_SUCCESSFUL:
        return r
    return user32.ChangeDisplaySettingsExW(None, None, None, 0, None)


def turn_off():
    """Stacca lo schermo virtuale dal desktop, ricordando risoluzione e posizione."""
    dd = find_virtual()
    if not dd or not dd.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
        return True
    dm = _current_mode(dd.DeviceName)
    if dm and dm.dmPelsWidth:
        STATE_FILE.write_text(json.dumps({
            "width": dm.dmPelsWidth, "height": dm.dmPelsHeight,
            "x": dm.dmPositionX, "y": dm.dmPositionY}))
    r = _apply(dd.DeviceName, 0, 0, 0, 0)
    return r == DISP_CHANGE_SUCCESSFUL and not is_on()


def turn_on(timeout=5.0):
    """Riattacca lo schermo virtuale con l'ultima risoluzione/posizione usata."""
    dd = find_virtual()
    if dd is None:
        return False
    if dd.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
        return True
    try:
        mode = {**DEFAULT_MODE, **json.loads(STATE_FILE.read_text())}
    except (OSError, ValueError):
        mode = dict(DEFAULT_MODE)
    if mode["x"] is None:  # a destra dello schermo principale
        p = _primary()
        pm = _current_mode(p.DeviceName) if p else None
        mode["x"] = pm.dmPelsWidth if pm else 1920
    r = _apply(dd.DeviceName, mode["width"], mode["height"], mode["x"], mode["y"])
    if r != DISP_CHANGE_SUCCESSFUL:
        # risoluzione salvata non più disponibile (es. tolta dal driver): usa la più grande
        modes = available_modes()
        if modes:
            w, h = max(modes, key=lambda m: m[0] * m[1])
            _apply(dd.DeviceName, w, h, mode["x"], mode["y"])
    end = time.time() + timeout
    while time.time() < end:
        if is_on():
            time.sleep(0.5)  # lascia a Windows il tempo di sistemare il desktop
            return True
        time.sleep(0.2)
    return False


def available_modes():
    dd = find_virtual()
    modes, i = set(), 0
    while dd:
        dm = DEVMODEW(dmSize=ctypes.sizeof(DEVMODEW))
        if not user32.EnumDisplaySettingsW(dd.DeviceName, i, ctypes.byref(dm)):
            break
        modes.add((dm.dmPelsWidth, dm.dmPelsHeight))
        i += 1
    return sorted(modes)


def set_resolution(width, height):
    """Cambia la risoluzione dello schermo virtuale (e la ricorda per la prossima volta)."""
    dd = find_virtual()
    if not dd:
        return False
    if not is_on():
        STATE_FILE.write_text(json.dumps({**_saved_mode(), "width": width, "height": height}))
        return True
    dm = _current_mode(dd.DeviceName)
    ok = _apply(dd.DeviceName, width, height, dm.dmPositionX, dm.dmPositionY) == DISP_CHANGE_SUCCESSFUL
    if ok:
        STATE_FILE.write_text(json.dumps({"width": width, "height": height,
                                          "x": dm.dmPositionX, "y": dm.dmPositionY}))
    return ok


def _saved_mode():
    try:
        return {**DEFAULT_MODE, **json.loads(STATE_FILE.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULT_MODE)


if __name__ == "__main__":
    import sys
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    dd = find_virtual()
    if dd is None:
        print("Virtual Display Driver non trovato")
    elif cmd == "on":
        print("acceso" if turn_on() else "ERRORE: non sono riuscito ad accenderlo")
    elif cmd == "off":
        print("spento" if turn_off() else "ERRORE: non sono riuscito a spegnerlo")
    elif cmd == "modes":
        print(", ".join(f"{w}x{h}" for w, h in available_modes()))
    elif cmd == "res" and len(sys.argv) == 4:
        w, h = int(sys.argv[2]), int(sys.argv[3])
        if (w, h) not in available_modes():
            print(f"{w}x{h} non disponibile. Disponibili: "
                  + ", ".join(f"{a}x{b}" for a, b in available_modes())
                  + "\nSe l'hai appena aggiunta a vdd_settings.xml, esegui riavvia-driver.bat")
        else:
            print(f"impostato {w}x{h}" if set_resolution(w, h) else "ERRORE")
    else:
        m = _current_mode(dd.DeviceName)
        print(dd.DeviceName, dd.DeviceString, "ACCESO" if is_on() else "SPENTO",
              f"{m.dmPelsWidth}x{m.dmPelsHeight} @ {m.dmPositionX},{m.dmPositionY}" if m else "")
