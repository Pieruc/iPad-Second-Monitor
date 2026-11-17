"""
Collegamento via cavo USB con l'app "Secondo Schermo" sull'iPad.

Usa il servizio Apple per l'USB (usbmuxd, installato con l'app "Apple Devices"
o con iTunes), che su Windows ascolta su 127.0.0.1:27015 e permette di aprire
una connessione TCP verso una porta dell'iPad lungo il cavo.

Protocollo con l'app (uguale in Link.swift):
    1 byte tipo + 4 byte lunghezza (big endian) + dati
    PC -> iPad: 1 = hello (JSON), 2 = immagine JPEG
    iPad -> PC: 10 = messaggio JSON
"""

import json
import plistlib
import socket
import struct
import threading
import time

USBMUXD = ("127.0.0.1", 27015)
APP_PORT = 7878
T_HELLO, T_FRAME, T_JSON = 1, 2, 10
CLIENT = {"ClientVersionString": "ipad-second-screen", "ProgName": "ipad-second-screen"}


class UsbmuxError(Exception):
    pass


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connessione chiusa")
        buf += chunk
    return bytes(buf)


def _mux_request(sock, obj, tag):
    payload = plistlib.dumps(obj)
    sock.sendall(struct.pack("<IIII", 16 + len(payload), 1, 8, tag) + payload)
    length, _version, _msg, _tag = struct.unpack("<IIII", _recv_exact(sock, 16))
    return plistlib.loads(_recv_exact(sock, length - 16))


def _mux_socket():
    try:
        return socket.create_connection(USBMUXD, timeout=3)
    except OSError:
        raise UsbmuxError(
            "servizio Apple per l'USB non trovato: installa l'app 'Apple Devices' "
            "dal Microsoft Store (oppure iTunes)")


def list_usb_devices():
    with _mux_socket() as s:
        r = _mux_request(s, {"MessageType": "ListDevices", **CLIENT}, 1)
    return [d for d in r.get("DeviceList", [])
            if d.get("Properties", {}).get("ConnectionType") == "USB"]


def connect(device_id, port=APP_PORT):
    """Apre una connessione TCP verso `port` sull'iPad, attraverso il cavo."""
    s = _mux_socket()
    r = _mux_request(s, {"MessageType": "Connect", "DeviceID": device_id,
                         "PortNumber": socket.htons(port), **CLIENT}, 2)
    if r.get("Number", 1) != 0:
        s.close()
        raise ConnectionRefusedError(r.get("Number"))
    s.settimeout(None)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return s


def _send_msg(sock, lock, mtype, data):
    with lock:
        sock.sendall(struct.pack(">BI", mtype, len(data)) + data)


def _session(sock, cap, on_message):
    """Gestisce una connessione con l'app finché resta aperta."""
    lock = threading.Lock()
    ready = threading.Event()
    ready.set()
    alive = threading.Event()
    alive.set()

    _send_msg(sock, lock, T_HELLO,
              json.dumps({"w": cap.mon["width"], "h": cap.mon["height"]}).encode())

    def receiver():
        try:
            while alive.is_set():
                mtype, length = struct.unpack(">BI", _recv_exact(sock, 5))
                data = _recv_exact(sock, length) if length else b""
                if mtype != T_JSON:
                    continue
                try:
                    m = json.loads(data)
                except ValueError:
                    continue
                if on_message(m):
                    ready.set()  # l'app ha mostrato l'immagine: si può inviare la prossima
        except (OSError, ConnectionError, struct.error):
            pass
        finally:
            alive.clear()
            ready.set()

    threading.Thread(target=receiver, daemon=True).start()

    last = 0
    while alive.is_set():
        ready.wait(timeout=3)  # se l'ack si perde, dopo 3 s si riparte comunque
        if not alive.is_set():
            break
        fid, data = cap.wait_new(last, 1.0)
        if data is None or fid == last:
            continue
        last = fid
        ready.clear()
        try:
            _send_msg(sock, lock, T_FRAME, data)
        except OSError:
            break
    alive.clear()


def run_usb(cap, on_message):
    print("\nModalità cavo USB: collega l'iPad e apri l'app 'Secondo Schermo'.")
    print("Ctrl+C per chiudere.\n")
    last_msg = None

    def status(msg):
        nonlocal last_msg
        if msg != last_msg:
            print(msg)
            last_msg = msg

    while True:
        try:
            devices = list_usb_devices()
            if not devices:
                status("In attesa dell'iPad sul cavo USB… (sblocca l'iPad e tocca 'Autorizza' se richiesto)")
                time.sleep(2)
                continue
            dev = devices[0]
            try:
                sock = connect(dev["DeviceID"])
            except ConnectionRefusedError:
                status("iPad collegato: apri l'app 'Secondo Schermo'…")
                time.sleep(1.5)
                continue
            print("[+] iPad connesso via cavo")
            last_msg = None
            try:
                _session(sock, cap, on_message)
            finally:
                sock.close()
                print("[-] iPad scollegato")
        except UsbmuxError as e:
            status(f"[!] {e}")
            time.sleep(3)
        except (OSError, ConnectionError) as e:
            status(f"[!] errore USB: {e}")
            time.sleep(2)
