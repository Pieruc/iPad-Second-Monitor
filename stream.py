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

from video_ffmpeg import FfmpegVideo
from PIL import Image, ImageDraw

user32 = ctypes.windll.user32
_MSS = getattr(mss, "MSS", None) or mss.mss

_last_printed = {}

# Stato leggibile dall'esterno (stato.json): modalità, capacità del client, contatori
STATUS_FILE = __import__("pathlib").Path(__file__).parent / "stato.json"
STATUS = {"client": None, "modalita": "zone JPEG", "video_disponibile": None,
          "fotogrammi_video": 0, "aggiornamenti_jpeg": 0, "ultimo_errore_video": None}
_status_saved = 0.0


def status(force=False, **kw):
    """Aggiorna stato.json (al massimo una volta al secondo, salvo force)."""
    global _status_saved
    STATUS.update(kw)
    now = time.monotonic()
    if force or now - _status_saved > 1.0:
        _status_saved = now
        try:
            STATUS_FILE.write_text(__import__("json").dumps(STATUS, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
        except OSError:
            pass


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


def dxgi_location(mon):
    """(indice scheda video, indice uscita) del monitor, come li vede DXGI; None se non trovato."""
    try:
        import dxcam
        factory = getattr(dxcam, "__factory")
        for d, outs in enumerate(factory.outputs):
            for o_idx, o in enumerate(outs):
                mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
                user32.GetMonitorInfoW(ctypes.c_void_p(o.hmonitor), ctypes.byref(mi))
                if (mi.rcMonitor.left, mi.rcMonitor.top) == (mon["left"], mon["top"]):
                    return d, o_idx
    except Exception:
        pass
    return None


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
        self._wrap_duplicator()

    def _wrap_duplicator(self):
        """
        Windows segnala un "nuovo fotogramma" anche quando si muove solo il mouse.
        In quel caso (AccumulatedFrames == 0) lo schermo non è cambiato: evitiamo di
        copiare e confrontare 15 MB di pixel.
        """
        dup = getattr(self.cam, "_duplicator", None)
        if dup is None or getattr(dup, "_solo_puntatore", False):
            return
        orig = dup.update_frame

        def update_frame(wait_for_frame=False):
            ok = orig(wait_for_frame=wait_for_frame)
            if ok and getattr(dup, "updated", False) and getattr(dup, "accumulated_frames", 1) == 0:
                dup.release_frame()
                dup.updated = False
            return ok

        dup.update_frame = update_frame
        dup._solo_puntatore = True

    def close(self):
        try:
            self.cam.release()  # permette di ricrearla con una nuova risoluzione
        except Exception:
            pass

    def grab(self):
        self._wrap_duplicator()  # dxcam può ricreare il duplicatore (cambio risoluzione)
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
        self.light = False         # True durante il video: cattura Python a 4 al secondo
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
            if self.light and t0 - getattr(self, "_last_grab", 0) < 0.25:
                img, changed = None, False   # in video basta sapere se lo schermo si muove ancora
            else:
                self._last_grab = t0
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
                    self._count = getattr(self, "_count", 0) + 1
                    notify = True
                if cur != self.cursor:
                    self.cursor = cur
                    self.cursor_id += 1
                    notify = True
                if notify:
                    self.cond.notify_all()

            now = time.monotonic()
            if now - getattr(self, "_count_t", 0) >= 2:
                status(cattura_fps=round(getattr(self, "_count", 0) / max(1e-6, now - getattr(self, "_count_t", now - 2)), 1))
                self._count, self._count_t = 0, now
            dt = time.perf_counter() - t0
            if dt < self.interval:
                time.sleep(self.interval - dt)

    def wait_change(self, frame_id, cursor_id, want_frames, timeout, extra=None):
        """
        Aspetta un nuovo frame (se want_frames) o un movimento del puntatore.
        want_frames può essere una funzione: viene rivalutata a ogni risveglio
        (es. quando arriva la conferma dell'iPad, vedi kick()).
        """
        def ready():
            want = want_frames() if callable(want_frames) else want_frames
            return ((want and self.frame_id != frame_id) or self.cursor_id != cursor_id
                    or (extra is not None and extra()))
        with self.cond:
            self.cond.wait_for(ready, timeout)
            return self.frame_id, self.raw, self.cursor_id, self.cursor

    def kick(self):
        """Sveglia chi aspetta in wait_change (es. è arrivata una conferma)."""
        with self.cond:
            self.cond.notify_all()


class Flow:
    """
    Controllo di flusso: al massimo `window` aggiornamenti in viaggio senza conferma.
    Con 2 in viaggio l'attesa dell'iPad non blocca l'invio del successivo.
    """
    def __init__(self, cap, window=2, timeout=3.0):
        self.cap, self.window, self.timeout = cap, window, timeout
        self.sent = []
        self.lock = threading.Lock()

    def can_send(self):
        now = time.monotonic()
        with self.lock:
            self.sent = [t for t in self.sent if now - t < self.timeout]  # conferme perse
            return len(self.sent) < self.window

    def on_send(self):
        with self.lock:
            self.sent.append(time.monotonic())

    def on_ack(self):
        with self.lock:
            t = self.sent.pop(0) if self.sent else None
        if t is not None:
            ms = (time.monotonic() - t) * 1000
            prev = STATUS.get("conferma_ipad_ms") or ms
            status(conferma_ipad_ms=round(prev * 0.8 + ms * 0.2, 1))
        kick = getattr(self.cap, "kick", None)
        if kick:
            kick()


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
    VIDEO_ENTER = 0.15  # passa al video se va rifatto >15% dello schermo (es. video in finestra)...
    VIDEO_ENTER_N = 3   # ...in almeno 3 aggiornamenti...
    VIDEO_WINDOW = 0.8  # ...nell'arco di 0,8 s
    VIDEO_STAY = 0.02   # resta in video finché cambia almeno il 2% dei pixel...
    VIDEO_EXIT = 1.5    # ...altrimenti dopo 1,5 s torna alle zone
    VIDEO_WARM = 20     # FFmpeg resta pronto 20 s dopo l'ultimo uso, poi si chiude (libera ~300 MB)

    def __init__(self, cap):
        self.cap = cap
        self.overlay = False
        self.video_ok = False     # il client sa decodificare H.264
        self.video = None         # FfmpegVideo (processo FFmpeg)
        self.video_on = False     # si sta mandando video
        self.video_pending = False
        self.pending_since = 0.0
        self.video_idle_since = 0.0
        self.big_changes = []     # istanti degli ultimi aggiornamenti "grandi"
        self.busy_at = 0.0
        self.last_raw = None
        self.frame_id = 0
        self.cursor_id = 0
        self.size_sent = None
        self.last_cursor_msg = None

    def set_overlay(self, on, video=False):
        self._video_off()
        self.video_ok = bool(on and video)
        self.overlay = bool(on)
        desc = ("zone + puntatore" if on else "immagini intere") + \
               (", video H.264: sì" if self.video_ok else ", video H.264: no")
        print_once("client", f"Client: {desc}")
        status(force=True, client=desc)
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
          ("json", dict), ("bin", bytes) oppure ("video", bytes)
        "bin" va inviato solo se can_send_frame (controllo di flusso); "video" sempre.
        """
        cap = self.cap
        video = self.video
        extra = video.ready if (video is not None and (self.video_on or self.video_pending)) else None
        fid, raw, cid, cursor = cap.wait_change(self.frame_id, self.cursor_id,
                                                can_send_frame, timeout, extra)
        if callable(can_send_frame):
            can_send_frame = can_send_frame()
        out = []
        if not self.overlay:
            if can_send_frame and raw is not None and (fid != self.frame_id or cid != self.cursor_id):
                self.frame_id, self.cursor_id = fid, cid
                out.append(("bin", self._full_with_cursor(raw, cursor)))
            return out

        now = time.monotonic()
        if cid != self.cursor_id:
            self.cursor_id = cid
            msg = self.cursor_message(cursor)
            if msg != self.last_cursor_msg:
                self.last_cursor_msg = msg
                out.append(("json", msg))

        # il video era stato richiesto: appena FFmpeg manda il primo fotogramma chiave si passa al video
        if self.video_pending and video is not None:
            if video.ready():
                self.video_pending = False
                self.video_on = True
                self.busy_at = now
                cap.light = True
                label = "Intel Quick Sync" if video.encoder == "h264_qsv" else "NVIDIA"
                print_once("mode", f"Modalità: video H.264 ({label})")
                status(force=True, modalita=f"video {label}", video_disponibile=True)
            elif not video.alive() or now - self.pending_since > 6:
                self._video_failed(video)

        if self.video_on:
            if raw is not None and fid != self.frame_id:
                self.frame_id = fid
                if self.scaled_size(raw) != self.size_sent:
                    self._video_off()
                else:
                    if change_fraction(self.last_raw, raw) >= self.VIDEO_STAY:
                        self.busy_at = now
                    self.last_raw = raw
            if self.video_on and not video.alive():
                self._video_failed(video)
            elif self.video_on and now - self.busy_at > self.VIDEO_EXIT:
                self._video_off()   # schermo fermo: torna alle zone
            if self.video_on:
                frames = video.pop_all()
                if frames:
                    status(fotogrammi_video=STATUS["fotogrammi_video"] + len(frames))
                out.extend(("video", f) for f in frames)
                return out
            # uscita dal video: immagine intera nitida
            self.last_raw = None
            if raw is not None:
                self.frame_id = fid
                out.append(("bin", self._patches(raw)))
            return out

        if can_send_frame and raw is not None and fid != self.frame_id:
            self.frame_id = fid
            size = self.scaled_size(raw)
            if size != self.size_sent:
                self.size_sent = size
                self.last_raw = None
                out.append(("json", {"t": "size", "w": size[0], "h": size[1]}))
            out.append(("bin", self._frame(raw)))

        # FFmpeg tenuto pronto per un po' dopo l'uso, poi chiuso (risparmio)
        if (video is not None and not self.video_on and not self.video_pending
                and now - self.video_idle_since > self.VIDEO_WARM):
            video.close()
            self.video = None
        return out

    def keepalive(self):
        """Messaggio leggero per far sapere al client che il PC è vivo."""
        if self.overlay:
            return ("json", self.cursor_message(self.cap.cursor))
        return None

    # ------------------------------------------------------------ video
    def _video_off(self):
        if self.video is not None:
            self.video.deactivate()
        if self.video_on or self.video_pending:
            print_once("mode", "Modalità: zone cambiate (JPEG)")
            status(force=True, modalita="zone JPEG")
        self.video_on = False
        self.video_pending = False
        self.video_idle_since = time.monotonic()
        self.cap.light = False

    def _video_failed(self, video):
        err = video.error or "FFmpeg si è chiuso"
        print(f"[!] video non disponibile ({err}): resto con le zone JPEG")
        status(force=True, video_disponibile=False, ultimo_errore_video=err)
        video.close()
        self.video = None
        self._video_off()
        if FfmpegVideo.all_failed:
            self.video_ok = False

    def _request_video(self):
        """Chiede a FFmpeg di iniziare a mandare video (intanto si continua con le zone)."""
        if self.video is None or not self.video.alive():
            loc = dxgi_location(self.cap.mon)
            if loc is None:
                self.video_ok = False
                return
            fps = max(10, round(1 / self.cap.interval))
            self.video = FfmpegVideo(loc[0], loc[1], fps, 10_000_000, self.cap.kick)
        self.video.activate()
        self.video_pending = True
        self.pending_since = time.monotonic()

    def _frame(self, raw):
        now = time.monotonic()
        h, w = raw.shape[:2]
        rects = changed_rects(self.last_raw, raw)
        # area che andrebbe ricompressa in JPEG (conta più dei pixel cambiati:
        # scorrendo una pagina cambiano pochi pixel ma su quasi tutto lo schermo)
        area = 1.0 if rects is None else sum((r[2] - r[0]) * (r[3] - r[1]) for r in rects) / (w * h)
        if self.video_ok and not self.video_pending and not FfmpegVideo.all_failed and area >= self.VIDEO_ENTER:
            self.big_changes = [t for t in self.big_changes if now - t < self.VIDEO_WINDOW] + [now]
            if len(self.big_changes) >= self.VIDEO_ENTER_N:
                self.big_changes = []
                self._request_video()
        status(aggiornamenti_jpeg=STATUS["aggiornamenti_jpeg"] + 1, ultima_area_cambiata=round(area, 3))
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
