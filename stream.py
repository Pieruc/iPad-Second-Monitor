"""
Cattura dello schermo e codifica efficiente per l'iPad.

Per i client aggiornati (pagina web e app recenti) si inviano:
  - solo le ZONE CAMBIATE dello schermo (rettangoli JPEG), non l'intero schermo;
  - la POSIZIONE del puntatore come piccolo messaggio JSON: lo disegna l'iPad,
    quindi muovere il mouse non costa nessuna compressione JPEG.
I client vecchi ricevono ancora immagini intere con il puntatore disegnato.

Formato del messaggio "patch" (binario):
  b"P" + n (uint16 LE) + n * [x (uint16 LE), y (uint16 LE), lunghezza (uint32 LE), JPEG]
"""

import ctypes
import io
import struct
import threading
import time
import zlib
from ctypes import wintypes

import mss
import numpy as np
from PIL import Image, ImageDraw

user32 = ctypes.windll.user32
_MSS = getattr(mss, "MSS", None) or mss.mss

_last_printed = {}


def print_once(key, text):
    """Stampa `text` solo se è diverso dall'ultimo messaggio dello stesso tipo."""
    if _last_printed.get(key) != text:
        _last_printed[key] = text
        print(text)


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def cursor_pos():
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


# Freccia del puntatore (unità: pixel a scala 1)
CURSOR_SHAPE = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]
CURSOR_UNITS = 20  # altezza della freccia nelle unità sopra


def cursor_polygon(x, y, k):
    return [(x + px * k, y + py * k) for px, py in CURSOR_SHAPE]


# --------------------------------------------------------------------------- #
# Sorgenti: restituiscono array numpy BGRA (altezza, larghezza, 4)
# --------------------------------------------------------------------------- #
class MssSource:
    """Cattura GDI (sempre disponibile, più lenta ma affidabile)."""
    name = "mss (GDI)"

    def __init__(self, mon):
        self.mon = mon
        self.sct = _MSS()
        self.crc = None

    def _shot(self):
        shot = self.sct.grab(self.mon)
        w, h = shot.size
        return np.frombuffer(shot.bgra, np.uint8).reshape(h, w, 4), shot.bgra

    def grab_always(self):
        return self._shot()[0]

    def grab(self):
        arr, raw = self._shot()
        crc = zlib.crc32(raw)
        if crc == self.crc:
            return None, False
        self.crc = crc
        return arr, True

    def close(self):
        pass


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class DxcamSource:
    """Cattura DXGI Desktop Duplication (veloce, riceve solo i frame cambiati)."""
    name = "dxcam (DXGI)"

    def __init__(self, mon):
        import dxcam
        factory = getattr(dxcam, "__factory")  # getattr evita il name-mangling di Python
        target = None
        for d, outs in enumerate(factory.outputs):
            for o_idx, o in enumerate(outs):
                mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
                user32.GetMonitorInfoW(ctypes.c_void_p(o.hmonitor), ctypes.byref(mi))
                if (mi.rcMonitor.left, mi.rcMonitor.top) == (mon["left"], mon["top"]):
                    target = (d, o_idx)
        if target is None:
            raise RuntimeError("monitor non trovato da dxcam")
        self.cam = dxcam.create(device_idx=target[0], output_idx=target[1],
                                processor_backend="numpy", output_color="BGRA")

    def close(self):
        try:
            self.cam.release()  # permette di ricrearla con una nuova risoluzione
        except Exception:
            pass

    def grab(self):
        arr = self.cam.grab()  # None se lo schermo non è cambiato
        if arr is None:
            return None, False
        return np.ascontiguousarray(arr), True


def _thumb_diff(a, b):
    """Differenza media (0-255) tra due schermate, calcolata su un campione."""
    if a.shape != b.shape:
        return 255
    sa = a[::16, ::16, :3].astype(np.int16)
    sb = b[::16, ::16, :3].astype(np.int16)
    return float(np.abs(sa - sb).mean())


class RobustSource:
    """
    Usa dxcam se possibile, ma controlla periodicamente con mss che non si sia
    "congelato" (succede con schermi virtuali / doppia GPU). In caso di errori o
    blocchi passa definitivamente a mss, così lo streaming non si ferma.
    """
    CHECK_EVERY = 2.0  # secondi senza frame da dxcam prima di verificare

    def __init__(self, mon, use_dxcam=True):
        self.mon = mon
        self.mss = MssSource(mon)
        self.dx = None
        if use_dxcam:
            try:
                self.dx = DxcamSource(mon)
            except Exception as e:
                print(f"dxcam non disponibile ({e}); uso mss")
        self.last = None
        self.last_new = time.monotonic()

    @property
    def name(self):
        return self.dx.name if self.dx else self.mss.name

    def close(self):
        if self.dx:
            self.dx.close()

    def _fallback(self, why):
        print(f"[!] dxcam {why}: passo alla cattura mss")
        self.dx = None

    def grab(self):
        if self.dx is None:
            img, changed = self.mss.grab()
        else:
            try:
                img, changed = self.dx.grab()
            except Exception as e:
                self._fallback(f"errore ({e})")
                img, changed = self.mss.grab()
            else:
                now = time.monotonic()
                if changed:
                    self.last_new = now
                elif self.last is not None and now - self.last_new > self.CHECK_EVERY:
                    # dxcam non dà frame da un po': lo schermo è davvero fermo?
                    self.last_new = now
                    real = self.mss.grab_always()
                    if _thumb_diff(real, self.last) > 3:
                        self._fallback("bloccato")
                        img, changed = real, True
        if img is not None:
            self.last = img
        return img, changed


# --------------------------------------------------------------------------- #
# Cattura in un thread: tiene l'ultima schermata e la posizione del puntatore
# --------------------------------------------------------------------------- #
class Capturer:
    def __init__(self, mon, fps, quality, scale, draw_cursor=True, use_dxcam=True, rect_fn=None):
        self.mon = mon             # dict condiviso con InputController: aggiornato sul posto
        self.rect_fn = rect_fn     # restituisce il rettangolo attuale del monitor (o None)
        self.interval = 1.0 / fps
        self.quality = quality
        self.scale = scale
        self.draw_cursor = draw_cursor
        self.use_dxcam = use_dxcam
        self.cursor_size = 1.0     # moltiplicatore (--cursor-size)
        self.raw = None            # ultima schermata (numpy BGRA)
        self.frame_id = 0
        self.cursor = None         # (x, y) normalizzati 0..1, o None se fuori schermo
        self.cursor_id = 0
        self.cond = threading.Condition()
        self.running = True
        threading.Thread(target=self._run, daemon=True).start()

    @property
    def cursor_k(self):
        # Circa 2x a 1640 righe (come un puntatore di Windows al 200%)
        return max(1.0, self.mon["height"] / 800) * self.cursor_size

    def _run(self):
        # Se il ciclo va in errore lo riavvia, invece di lasciare lo schermo congelato
        while self.running:
            try:
                self._loop()
            except Exception as e:
                print(f"[!] errore di cattura, riavvio: {e!r}")
                time.sleep(0.5)

    def _check_geometry(self):
        """True se risoluzione/posizione del monitor sono cambiate (e aggiorna self.mon)."""
        if self.rect_fn is None:
            return False
        try:
            r = self.rect_fn()
        except Exception:
            return False
        keys = ("left", "top", "width", "height")
        if not r or all(r[k] == self.mon[k] for k in keys):
            return False
        self.mon.update({k: r[k] for k in keys})
        print(f"Schermo cambiato: ora {r['width']}x{r['height']} @ ({r['left']},{r['top']})")
        return True

    def _loop(self):
        source = RobustSource(self.mon, self.use_dxcam)
        print_once("capture", f"Metodo di cattura: {source.name}")
        last_geo_check = time.perf_counter()
        while self.running:
            t0 = time.perf_counter()
            if t0 - last_geo_check > 1.0:
                last_geo_check = t0
                if self._check_geometry():
                    source.close()
                    return  # _run ricrea la sorgente con le nuove dimensioni
            img, changed = source.grab()
            cx, cy = cursor_pos()
            w, h = self.mon["width"], self.mon["height"]
            rx, ry = cx - self.mon["left"], cy - self.mon["top"]
            cur = (rx / w, ry / h) if (0 <= rx < w and 0 <= ry < h) else None

            if changed and img is not None and same_frame(self.raw, img):
                changed = False   # cambiato solo il puntatore: niente da ricomprimere
            notify = False
            with self.cond:
                if changed and img is not None:
                    self.raw = img
                    self.frame_id += 1
                    notify = True
                if cur != self.cursor:
                    self.cursor = cur
                    self.cursor_id += 1
                    notify = True
                if notify:
                    self.cond.notify_all()

            dt = time.perf_counter() - t0
            if dt < self.interval:
                time.sleep(self.interval - dt)

    def wait_change(self, frame_id, cursor_id, want_frames, timeout):
        """Aspetta un nuovo frame (se want_frames) o un movimento del puntatore."""
        def ready():
            return (want_frames and self.frame_id != frame_id) or self.cursor_id != cursor_id
        with self.cond:
            self.cond.wait_for(ready, timeout)
            return self.frame_id, self.raw, self.cursor_id, self.cursor


# --------------------------------------------------------------------------- #
# Codifica
# --------------------------------------------------------------------------- #
BAND = 64     # altezza delle fasce in cui si cercano le modifiche
ALIGN = 16    # i rettangoli sono allineati a 16 px (blocchi JPEG puliti)


def _as_pairs(arr):
    """Vista (righe, larghezza/2) in uint64: confronta 2 pixel alla volta (più veloce)."""
    h, w = arr.shape[:2]
    if w % 2:
        return arr.view(np.uint32)[..., 0], 1
    return arr.reshape(-1).view(np.uint64).reshape(h, w // 2), 2


def same_frame(a, b):
    return a is not None and b is not None and a.shape == b.shape and         np.array_equal(_as_pairs(a)[0], _as_pairs(b)[0])


def changed_rects(old, new):
    """
    Rettangoli [x0, y0, x1, y1] delle zone cambiate tra due schermate.
    None se serve un'immagine intera (prima schermata o cambio di dimensioni).
    """
    if old is None or old.shape != new.shape:
        return None
    # confronto dei pixel a coppie, come interi a 64 bit (veloce)
    po, px = _as_pairs(old)
    pn, _ = _as_pairs(new)
    d = po != pn                      # (altezza, larghezza / px)
    if not d.any():
        return []
    h = d.shape[0]
    w = new.shape[1]
    starts = np.arange(0, h, BAND)
    bands = np.logical_or.reduceat(d, starts, axis=0)
    rows = np.flatnonzero(bands.any(axis=1))
    rects = []
    for i in rows:
        cols = np.flatnonzero(bands[i])
        x0 = int(cols[0]) * px // ALIGN * ALIGN
        x1 = min(w, -(-((int(cols[-1]) + 1) * px) // ALIGN) * ALIGN)
        y0 = int(starts[i])
        y1 = min(h, y0 + BAND)
        last = rects[-1] if rects else None
        if last and last[3] == y0 and x0 <= last[2] and x1 >= last[0]:
            last[0], last[2], last[3] = min(last[0], x0), max(last[2], x1), y1
        else:
            rects.append([x0, y0, x1, y1])
    # stringe ogni rettangolo in verticale sulle righe davvero cambiate
    for r in rects:
        ys = np.flatnonzero(d[r[1]:r[3], r[0] // px:-(-r[2] // px)].any(axis=1))
        y0 = r[1] + int(ys[0]) // ALIGN * ALIGN
        y1 = min(h, r[1] + -(-(int(ys[-1]) + 1) // ALIGN) * ALIGN)
        r[1], r[3] = y0, y1
    return rects


def change_fraction(old, new):
    """Frazione (0..1) dello schermo cambiata, stimata su una riga ogni 4 (veloce)."""
    if old is None or old.shape != new.shape:
        return 1.0
    po, pn = _as_pairs(old)[0], _as_pairs(new)[0]
    return float((po[::4] != pn[::4]).mean())


class NvencVideo:
    """
    Video H.264 compresso dalla scheda NVIDIA (NVENC). Riceve direttamente i pixel
    BGRA: conversione dei colori e compressione le fa la GPU, non il processore.
    Messaggio: b"V" + 1 byte (1 = fotogramma chiave) + flusso H.264 Annex B.
    """
    available = None  # None = non ancora provato

    def __init__(self, width, height, fps, bitrate=10_000_000):
        import fractions
        import av
        self.av = av
        cc = av.CodecContext.create("h264_nvenc", "w")
        cc.width, cc.height = width, height
        cc.pix_fmt = "bgra"
        cc.time_base = fractions.Fraction(1, fps)
        cc.framerate = fps
        cc.bit_rate = bitrate
        cc.gop_size = fps * 4        # un fotogramma chiave ogni 4 s (ripresa da errori)
        cc.max_b_frames = 0
        cc.options = {"preset": "p1", "tune": "ull", "zerolatency": "1", "delay": "0",
                      "profile": "high", "rc": "vbr", "maxrate": str(bitrate * 2)}
        cc.open()
        self.cc = cc
        self.width, self.height = width, height
        self.n = 0

    def encode(self, raw):
        frame = raw[:self.height, :self.width]
        if not frame.flags["C_CONTIGUOUS"]:
            frame = np.ascontiguousarray(frame)
        vf = self.av.VideoFrame.from_ndarray(frame, format="bgra")
        vf.pts = self.n
        self.n += 1
        packets = self.cc.encode(vf)
        data = b"".join(bytes(p) for p in packets)
        if not data:
            return None
        key = any(p.is_keyframe for p in packets)
        return b"V" + bytes([1 if key else 0]) + data

    def close(self):
        try:
            self.cc.encode(None)  # svuota il codificatore
        except Exception:
            pass
        self.cc = None


def encode_region(raw, x0, y0, x1, y1, quality, scale, draw=None):
    """Comprime in JPEG una zona della schermata. Restituisce (x, y, jpeg) in scala."""
    crop = np.ascontiguousarray(raw[y0:y1, x0:x1])
    img = Image.frombuffer("RGB", (x1 - x0, y1 - y0), crop, "raw", "BGRX", 0, 1)
    if draw:
        draw(img)
    if scale != 1.0:
        sx0, sy0 = round(x0 * scale), round(y0 * scale)
        sx1, sy1 = round(x1 * scale), round(y1 * scale)
        img = img.resize((max(1, sx1 - sx0), max(1, sy1 - sy0)), Image.BILINEAR)
        x0, y0 = sx0, sy0
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return x0, y0, buf.getvalue()


class StreamEncoder:
    """
    Stato di un client. `overlay=True` (client aggiornati): zone cambiate +
    puntatore disegnato dall'iPad. `overlay=False`: immagini intere col puntatore.
    """
    FULL_RATIO = 0.5    # oltre metà schermo cambiato: conviene l'immagine intera
    VIDEO_ENTER = 0.25  # passa al video se cambia >25% dello schermo...
    VIDEO_ENTER_N = 3   # ...in almeno 3 aggiornamenti...
    VIDEO_WINDOW = 0.8  # ...nell'arco di 0,8 s
    VIDEO_STAY = 0.02   # resta in video finché cambia almeno il 2% dei pixel...
    VIDEO_EXIT = 1.5    # ...altrimenti dopo 1,5 s torna alle zone (e la NVIDIA si riposa)

    def __init__(self, cap):
        self.cap = cap
        self.overlay = False
        self.video_ok = False     # il client sa decodificare H.264
        self.video = None         # NvencVideo attivo
        self.big_changes = []     # istanti degli ultimi aggiornamenti "grandi"
        self.busy_at = 0.0
        self.last_raw = None
        self.frame_id = 0
        self.cursor_id = 0
        self.size_sent = None
        self.last_cursor_msg = None

    def set_overlay(self, on, video=False):
        self._stop_video()
        self.video_ok = bool(on and video)
        self.overlay = bool(on)
        self.last_raw = None   # la prossima volta immagine intera
        self.frame_id = 0
        self.size_sent = None

    def scaled_size(self, raw):
        h, w = raw.shape[:2]
        s = self.cap.scale
        return (round(w * s), round(h * s)) if s != 1.0 else (w, h)

    def cursor_message(self, cursor):
        cap = self.cap
        visible = cursor is not None and cap.draw_cursor
        x, y = cursor if cursor else (0, 0)
        # "s": altezza della freccia rispetto all'altezza dello schermo
        s = CURSOR_UNITS * cap.cursor_k / max(1, cap.mon["height"])
        return {"t": "cur", "x": round(x, 5), "y": round(y, 5), "v": visible, "s": round(s, 5)}

    def poll(self, can_send_frame, timeout):
        """
        Restituisce un elenco di messaggi da inviare:
          ("json", dict) oppure ("bin", bytes)
        Il messaggio binario (immagine) va inviato solo se can_send_frame.
        """
        cap = self.cap
        fid, raw, cid, cursor = cap.wait_change(self.frame_id, self.cursor_id,
                                                can_send_frame, timeout)
        out = []
        if self.overlay:
            if cid != self.cursor_id:
                self.cursor_id = cid
                msg = self.cursor_message(cursor)
                if msg != self.last_cursor_msg:
                    self.last_cursor_msg = msg
                    out.append(("json", msg))
            if (can_send_frame and raw is not None and self.video is not None
                    and fid == self.frame_id and time.monotonic() - self.busy_at > self.VIDEO_EXIT):
                # lo schermo si è fermato: chiude il video e rimanda un'immagine nitida
                self._stop_video()
                self.last_raw = None
                out.append(("bin", self._patches(raw)))
            elif can_send_frame and raw is not None and fid != self.frame_id:
                self.frame_id = fid
                size = self.scaled_size(raw)
                if size != self.size_sent:
                    self.size_sent = size
                    self.last_raw = None
                    self._stop_video()
                    out.append(("json", {"t": "size", "w": size[0], "h": size[1]}))
                data = self._frame(raw)
                if data:
                    out.append(("bin", data))
        elif can_send_frame and raw is not None and (fid != self.frame_id or cid != self.cursor_id):
            self.frame_id, self.cursor_id = fid, cid
            out.append(("bin", self._full_with_cursor(raw, cursor)))
        return out

    def keepalive(self):
        """Messaggio leggero per far sapere al client che il PC è vivo."""
        if self.overlay:
            return ("json", self.cursor_message(self.cap.cursor))
        return None

    # ------------------------------------------------------------ video
    def _stop_video(self):
        if self.video is not None:
            self.video.close()
            self.video = None
            print_once("mode", "Modalità: zone cambiate (JPEG)")

    def _start_video(self, raw):
        if NvencVideo.available is False:
            return False
        h, w = raw.shape[:2]
        try:
            self.video = NvencVideo(w - w % 2, h - h % 2, max(10, round(1 / self.cap.interval)))
            NvencVideo.available = True
            print_once("mode", "Modalità: video H.264 (NVIDIA)")
            return True
        except Exception as e:
            NvencVideo.available = False
            self.video = None
            print(f"[!] video NVIDIA non disponibile ({e}): resto con le zone JPEG")
            return False

    def _frame(self, raw):
        now = time.monotonic()
        if self.video is not None:
            frac = change_fraction(self.last_raw, raw)
            self.last_raw = raw
            if frac >= self.VIDEO_STAY:
                self.busy_at = now
            if now - self.busy_at <= self.VIDEO_EXIT:
                try:
                    data = self.video.encode(raw)
                    if data:
                        return data
                except Exception as e:
                    print(f"[!] errore video: {e}")
                    NvencVideo.available = False
            # fine del video: torna alle zone con un'immagine intera nitida
            self._stop_video()
            self.last_raw = None
            return self._patches(raw)

        h, w = raw.shape[:2]
        rects = changed_rects(self.last_raw, raw)
        # area che andrebbe ricompressa in JPEG (conta più dei pixel cambiati:
        # scorrendo una pagina cambiano pochi pixel ma su quasi tutto lo schermo)
        area = 1.0 if rects is None else sum((r[2] - r[0]) * (r[3] - r[1]) for r in rects) / (w * h)
        if self.video_ok and NvencVideo.available is not False:
            if area >= self.VIDEO_ENTER:
                self.big_changes = [t for t in self.big_changes if now - t < self.VIDEO_WINDOW] + [now]
                if len(self.big_changes) >= self.VIDEO_ENTER_N and self._start_video(raw):
                    self.big_changes = []
                    self.busy_at = now
                    self.last_raw = raw
                    data = self.video.encode(raw)
                    if data:
                        return data
        return self._patches(raw, rects)

    # ------------------------------------------------------------ zone JPEG
    def _patches(self, raw, rects=False):
        cap = self.cap
        h, w = raw.shape[:2]
        if rects is False:
            rects = changed_rects(self.last_raw, raw)
        if rects is None or sum((r[2] - r[0]) * (r[3] - r[1]) for r in rects) > self.FULL_RATIO * w * h:
            rects = [[0, 0, w, h]]
        self.last_raw = raw
        parts = [b"P", struct.pack("<H", len(rects))]
        for x0, y0, x1, y1 in rects:
            x, y, jpeg = encode_region(raw, x0, y0, x1, y1, cap.quality, cap.scale)
            parts.append(struct.pack("<HHI", x, y, len(jpeg)))
            parts.append(jpeg)
        return b"".join(parts)

    def _full_with_cursor(self, raw, cursor):
        cap = self.cap
        h, w = raw.shape[:2]
        draw = None
        if cursor is not None and cap.draw_cursor:
            k = cap.cursor_k
            pts = cursor_polygon(cursor[0] * w, cursor[1] * h, k)

            def draw(img):
                ImageDraw.Draw(img).polygon(pts, fill="white", outline="black", width=max(1, int(k)))
        return encode_region(raw, 0, 0, w, h, cap.quality, cap.scale, draw)[2]
