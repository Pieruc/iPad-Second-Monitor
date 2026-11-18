"""
iPad Second Screen - server per Windows.

Cattura un monitor del PC e lo trasmette via WebSocket a Safari sull'iPad.
I tocchi sull'iPad vengono convertiti in eventi mouse sul PC.

Uso:
    python server.py                 # sceglie automaticamente il monitor (secondario se esiste)
    python server.py --list          # elenca i monitor
    python server.py --monitor 2     # usa il monitor 2
    python server.py --fps 30 --quality 70 --scale 1.0
"""

import argparse
import asyncio
import ctypes
import io
import json
import socket
import threading
import time
import zlib
from ctypes import wintypes
from pathlib import Path

import mss
from aiohttp import WSMsgType, web
from PIL import Image, ImageChops, ImageDraw, ImageStat

# --------------------------------------------------------------------------- #
# DPI awareness: coordinate fisiche coerenti tra mss e SendInput
# --------------------------------------------------------------------------- #
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

user32 = ctypes.windll.user32
_MSS = getattr(mss, "MSS", None) or mss.mss

# --------------------------------------------------------------------------- #
# Input (SendInput)
# --------------------------------------------------------------------------- #
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("_pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send_mouse(flags, dx=0, dy=0, data=0):
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(dx, dy, ctypes.c_ulong(data & 0xFFFFFFFF).value, flags, 0, 0)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


class InputController:
    def __init__(self, mon):
        self.mon = mon  # dict con left, top, width, height

    def _abs(self, nx, ny):
        nx = min(max(nx, 0.0), 1.0)
        ny = min(max(ny, 0.0), 1.0)
        px = self.mon["left"] + nx * (self.mon["width"] - 1)
        py = self.mon["top"] + ny * (self.mon["height"] - 1)
        vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        ax = int(round((px - vx) * 65535 / max(vw - 1, 1)))
        ay = int(round((py - vy) * 65535 / max(vh - 1, 1)))
        return ax, ay

    def move(self, nx, ny):
        ax, ay = self._abs(nx, ny)
        _send_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ax, ay)

    def button(self, nx, ny, which, down):
        self.move(nx, ny)
        if which == "right":
            flag = MOUSEEVENTF_RIGHTDOWN if down else MOUSEEVENTF_RIGHTUP
        else:
            flag = MOUSEEVENTF_LEFTDOWN if down else MOUSEEVENTF_LEFTUP
        _send_mouse(flag)

    def scroll(self, dx, dy):
        if dy:
            _send_mouse(MOUSEEVENTF_WHEEL, data=int(-dy))
        if dx:
            _send_mouse(MOUSEEVENTF_HWHEEL, data=int(dx))

    def handle(self, msg):
        t = msg.get("t")
        if t == "move":
            self.move(msg["x"], msg["y"])
        elif t == "down":
            self.button(msg["x"], msg["y"], msg.get("b", "left"), True)
        elif t == "up":
            self.button(msg["x"], msg["y"], msg.get("b", "left"), False)
        elif t == "click":
            b = msg.get("b", "left")
            self.button(msg["x"], msg["y"], b, True)
            self.button(msg["x"], msg["y"], b, False)
        elif t == "scroll":
            self.scroll(msg.get("dx", 0), msg.get("dy", 0))


# --------------------------------------------------------------------------- #
# Cattura schermo
# --------------------------------------------------------------------------- #
from stream import Capturer, Flow, StreamEncoder, print_once  # noqa: E402


# --------------------------------------------------------------------------- #
# Web server
# --------------------------------------------------------------------------- #
STATIC = Path(__file__).parent / "static"


async def index(request):
    return web.FileResponse(STATIC / "index.html")


def handle_client_message(m, cap, ctrl, cursor_allowed):
    """Gestisce un messaggio dall'iPad (web o app). Restituisce True se è un ack."""
    t = m.get("t")
    if t == "ack":
        return True
    if t == "info":
        # dimensione reale della finestra sull'iPad
        info = {k: m.get(k) for k in ("w", "h", "dpr", "fullscreen", "standalone", "fsApi",
                                      "secure", "wakeLock", "video", "ua")}
        info["schermo_pc"] = f'{cap.mon["width"]}x{cap.mon["height"]}'
        print_once("info", f'Finestra iPad: {info["w"]}x{info["h"]} '
                   f'(schermo intero: {"sì" if info["fullscreen"] or info["standalone"] else "no"}), '
                   f'schermo PC: {info["schermo_pc"]}, '
                   f'anti-standby: {info["wakeLock"]} / video {info["video"]}')
        try:
            (Path(__file__).parent / "ipad_info.json").write_text(json.dumps(info, indent=2))
        except OSError:
            pass
    elif t == "cursor":
        # l'iPad sceglie se disegnare il puntatore (tap con tre dita)
        cap.draw_cursor = bool(m.get("on")) and cursor_allowed
    else:
        try:
            ctrl.handle(m)
        except Exception as e:
            print("input error:", e)
    return False


async def ws_handler(request):
    app = request.app
    cap: Capturer = app["capturer"]
    ctrl: InputController = app["input"]
    ws = web.WebSocketResponse(max_msg_size=0, heartbeat=10)
    await ws.prepare(request)
    peer = request.remote
    print(f"[+] iPad connesso: {peer}")

    await ws.send_str(json.dumps({
        "t": "hello",
        "w": cap.mon["width"],
        "h": cap.mon["height"],
    }))

    loop = asyncio.get_running_loop()
    enc = StreamEncoder(cap)
    request["encoder"] = enc
    flow = Flow(cap)
    state = {"keep": time.monotonic()}

    async def sender():
        while not ws.closed:
            now = time.monotonic()
            msgs = await loop.run_in_executor(None, enc.poll, flow.can_send, 0.5)
            if not msgs and now - state["keep"] > 2:
                ka = enc.keepalive()
                msgs = [ka] if ka else []
            try:
                for kind, data in msgs:
                    if kind == "json":
                        await ws.send_str(json.dumps(data))
                    else:
                        flow.on_send()
                        await ws.send_bytes(data)
                    state["keep"] = time.monotonic()
            except Exception:
                break

    task = asyncio.create_task(sender())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    m = json.loads(msg.data)
                except ValueError:
                    continue
                if m.get("t") == "caps":
                    enc.set_overlay(m.get("patches") and m.get("cursor"), video="h264" in (m.get("video") or []))
                elif handle_client_message(m, cap, ctrl, app["cursor_allowed"]):
                    flow.on_ack()
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        task.cancel()
        print(f"[-] iPad disconnesso: {peer}")
    return ws


def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


_console_handler = None  # riferimento globale: evita che il callback venga liberato


def setup_virtual_display(manage=True):
    """
    Accende lo schermo virtuale all'avvio e lo spegne alla chiusura del programma
    (Ctrl+C, chiusura della finestra, logout/spegnimento del PC).
    Restituisce la posizione (left, top) dello schermo virtuale, o None.
    """
    global _console_handler
    import atexit
    import display

    dd = display.find_virtual()
    if dd is None:
        return None
    if manage:
        if not display.is_on():
            print("Accendo lo schermo virtuale…")
            if not display.turn_on():
                print("[!] non sono riuscito ad accendere lo schermo virtuale")
                return None

        done = []

        def cleanup():
            if not done:
                done.append(1)
                if display.turn_off():
                    print("Schermo virtuale spento.")

        atexit.register(cleanup)

        # Chiusura della finestra con la X: Windows chiama questo handler
        # e concede qualche secondo prima di terminare il processo.
        HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def on_console_event(event):
            if event in (2, 5, 6):  # CLOSE, LOGOFF, SHUTDOWN
                cleanup()
            return False  # lascia proseguire la gestione normale (Ctrl+C incluso)

        _console_handler = HANDLER(on_console_event)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler, True)

    dm = display._current_mode(dd.DeviceName)
    return (dm.dmPositionX, dm.dmPositionY) if dm and dm.dmPelsWidth else None


def main():
    import sys
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description="Usa l'iPad come secondo schermo")
    ap.add_argument("--list", action="store_true", help="elenca i monitor ed esci")
    ap.add_argument("--monitor", type=int, default=None, help="numero del monitor da trasmettere")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--quality", type=int, default=70, help="qualità JPEG 1-95")
    ap.add_argument("--scale", type=float, default=1.0, help="ridimensionamento (es. 0.75)")
    ap.add_argument("--no-cursor", action="store_true", help="non disegnare il cursore")
    ap.add_argument("--cursor-size", type=float, default=1.0,
                    help="grandezza del puntatore disegnato (es. 0.75 più piccolo, 1.5 più grande)")
    ap.add_argument("--no-dxcam", action="store_true", help="usa solo la cattura mss (più lenta)")
    ap.add_argument("--keep-display", action="store_true",
                    help="non accendere/spegnere automaticamente lo schermo virtuale")
    ap.add_argument("--usb", action="store_true",
                    help="collegamento via cavo USB con l'app 'Secondo Schermo' sull'iPad")
    ap.add_argument("--wifi-app", metavar="IP_IPAD",
                    help="collegamento via Wi-Fi con l'app 'Secondo Schermo' (indirizzo mostrato dall'app)")
    ap.add_argument("--code", help="codice di abbinamento a 6 cifre mostrato dall'app")
    ap.add_argument("--http", action="store_true",
                    help="usa HTTP semplice (senza certificato; l'iPad può andare in standby)")
    args = ap.parse_args()

    virtual_pos = None
    if not args.list:
        virtual_pos = setup_virtual_display(manage=not args.keep_display)

    with _MSS() as sct:
        mons = sct.monitors[1:]

    if args.list or not mons:
        for i, m in enumerate(mons, 1):
            print(f"  {i}: {m['width']}x{m['height']} @ ({m['left']},{m['top']})")
        return

    if args.monitor is None:
        # Preferisce lo schermo virtuale, altrimenti un monitor non principale
        idx = next((i for i, m in enumerate(mons, 1) if (m["left"], m["top"]) == virtual_pos), None)
        if idx is None:
            idx = next((i for i, m in enumerate(mons, 1) if (m["left"], m["top"]) != (0, 0)), 1)
    else:
        idx = args.monitor
    mon = dict(mons[idx - 1])

    print("Monitor disponibili:")
    for i, m in enumerate(mons, 1):
        mark = "  <-- trasmesso" if i == idx else ""
        print(f"  {i}: {m['width']}x{m['height']} @ ({m['left']},{m['top']}){mark}")

    # Funzione che restituisce sempre la geometria attuale del monitor trasmesso,
    # così cambi di risoluzione/posizione a programma avviato vengono seguiti.
    if virtual_pos is not None and (mon["left"], mon["top"]) == virtual_pos:
        import display

        def rect_fn():
            dd = display.find_virtual()
            dm = display._current_mode(dd.DeviceName) if dd and display.is_on() else None
            if not dm or not dm.dmPelsWidth:
                return None
            return {"left": dm.dmPositionX, "top": dm.dmPositionY,
                    "width": dm.dmPelsWidth, "height": dm.dmPelsHeight}
    else:
        def rect_fn():
            with _MSS() as sct:
                ms = sct.monitors[1:]
            return ms[idx - 1] if idx - 1 < len(ms) else None

    app = web.Application()
    app["capturer"] = Capturer(mon, args.fps, args.quality, args.scale, not args.no_cursor,
                               not args.no_dxcam, rect_fn)
    app["capturer"].cursor_size = args.cursor_size
    app["input"] = InputController(mon)
    app["cursor_allowed"] = not args.no_cursor
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static", STATIC)

    if args.usb or args.wifi_app:
        from usb_link import run_usb, run_wifi
        cap, ctrl = app["capturer"], app["input"]
        handler = lambda m: handle_client_message(m, cap, ctrl, app["cursor_allowed"])  # noqa: E731
        try:
            if args.wifi_app:
                if not (args.code and args.code.isdigit() and len(args.code) == 6):
                    print("[!] serve il codice a 6 cifre mostrato dall'app: --code 123456")
                    return
                run_wifi(cap, handler, args.wifi_app, args.code)
            else:
                run_usb(cap, handler)
        except KeyboardInterrupt:
            pass
        return

    ips = local_ips()
    if args.http:
        print("\nApri su Safari dell'iPad uno di questi indirizzi:")
        for ip in ips:
            print(f"   http://{ip}:{args.port}")
        print("\nCtrl+C per chiudere.\n")
        web.run_app(app, host="0.0.0.0", port=args.port, print=None)
        return

    import ssl
    from certs import CA_CRT, ca_mobileconfig, ensure_server_cert
    crt, key = ensure_server_cert(ips)
    ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_ctx.load_cert_chain(crt, key)

    async def ca_cert(request):
        return web.FileResponse(CA_CRT, headers={"Content-Type": "application/x-x509-ca-cert"})

    async def ca_profile(request):
        # Profilo di configurazione iOS: Safari lo riconosce e propone l'installazione
        return web.Response(body=ca_mobileconfig(), headers={
            "Content-Type": "application/x-apple-aspen-config",
            "Content-Disposition": 'inline; filename="iPad-Second-Screen.mobileconfig"',
        })

    async def setup_page(request):
        return web.FileResponse(STATIC / "setup.html")

    app.router.add_get("/ca.crt", ca_cert)
    # Piccolo server HTTP (senza certificato) solo per la configurazione iniziale
    setup = web.Application()
    setup["https_port"] = args.port
    setup.router.add_get("/", setup_page)
    setup.router.add_get("/ca.crt", ca_cert)
    setup.router.add_get("/ca.mobileconfig", ca_profile)
    setup_port = args.port + 1

    print("\nSull'iPad, con Safari:")
    for ip in ips:
        print(f"   schermo:            https://{ip}:{args.port}")
    for ip in ips:
        print(f"   prima configurazione: http://{ip}:{setup_port}")
    print("\nCtrl+C per chiudere.\n")

    async def run():
        r1 = web.AppRunner(app)
        r2 = web.AppRunner(setup)
        await r1.setup()
        await r2.setup()
        await web.TCPSite(r1, "0.0.0.0", args.port, ssl_context=ssl_ctx).start()
        await web.TCPSite(r2, "0.0.0.0", setup_port).start()
        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
