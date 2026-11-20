"""
Video H.264 catturato e compresso da FFmpeg (processo separato).

FFmpeg cattura lo schermo virtuale con le DirectX (ddagrab) e lo comprime con il
codificatore hardware: prima Media Foundation sul chip Intel (l'immagine non passa mai
dalla CPU), poi Intel Quick Sync, poi NVIDIA NVENC (la NVIDIA resta spenta se non serve). Lavorando in un processo a parte non rallenta la cattura in Python
(prima i due lavori si bloccavano a vicenda) e arriva a 60 fotogrammi al secondo.

Il flusso esce in formato FLV (ogni pacchetto ha la sua lunghezza, facile da
separare) e viene convertito in H.264 Annex B: b"V" + 1 byte chiave + dati.
"""

import ctypes
import struct
import subprocess
import threading
import time
from ctypes import wintypes

ENCODERS = [
    # nome, filtri dopo la cattura, opzioni
    # 1) Media Foundation (codificatore Intel di Windows): l'immagine resta nella scheda
    #    video dalla cattura alla compressione, quasi nessun lavoro per la CPU
    ("h264_mf", "", ["-hw_encoding", "1", "-scenario", "display_remoting", "-rate_control", "cbr"]),
    # 2) Quick Sync e NVENC: l'immagine passa dalla CPU per la conversione dei colori
    ("h264_qsv", "hwdownload,format=bgra,format=nv12",
     ["-preset", "veryfast", "-async_depth", "1", "-look_ahead", "0"]),
    ("h264_nvenc", "hwdownload,format=bgra",
     ["-preset", "p1", "-tune", "ull", "-zerolatency", "1", "-delay", "0"]),
]


def _ffmpeg_exe():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


# --- chiude FFmpeg automaticamente se il programma termina (Job object di Windows)
class _JobLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]


class _JobExtLimits(ctypes.Structure):
    _fields_ = [("Basic", _JobLimits), ("Io", _IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


_job = None


def _kill_with_parent(proc):
    global _job
    try:
        k32 = ctypes.windll.kernel32
        if _job is None:
            _job = k32.CreateJobObjectW(None, None)
            info = _JobExtLimits()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            k32.SetInformationJobObject(_job, 9, ctypes.byref(info), ctypes.sizeof(info))
        k32.AssignProcessToJobObject(_job, wintypes.HANDLE(int(proc._handle)))
    except Exception:
        pass


def _avcc_to_annexb(payload, length_size):
    out = bytearray()
    i = 0
    while i + length_size <= len(payload):
        n = int.from_bytes(payload[i:i + length_size], "big")
        i += length_size
        out += b"\x00\x00\x00\x01" + payload[i:i + n]
        i += n
    return bytes(out)


class FfmpegVideo:
    """Un processo FFmpeg che cattura e comprime; i fotogrammi si leggono con pop_all()."""
    chosen = None      # codificatore che ha funzionato (si riusa)
    all_failed = False

    def __init__(self, adapter, output, fps, bitrate, on_frame):
        self.adapter, self.output, self.fps, self.bitrate = adapter, output, fps, bitrate
        self.on_frame = on_frame
        self.lock = threading.Lock()
        self.frames = []
        self.active = False
        self.need_key = True
        self.proc = None
        self.closed = False
        self.encoder = None
        self.error = None
        threading.Thread(target=self._run, daemon=True).start()

    # ------------------------------------------------------------ comandi
    def activate(self):
        """Inizia a raccogliere fotogrammi (dal prossimo fotogramma chiave)."""
        with self.lock:
            self.frames.clear()
            self.need_key = True
            self.active = True

    def deactivate(self):
        with self.lock:
            self.active = False
            self.frames.clear()

    def pop_all(self):
        with self.lock:
            f, self.frames = self.frames, []
            return f

    def ready(self):
        with self.lock:
            return bool(self.frames)

    def alive(self):
        return not self.closed and (self.proc is None or self.proc.poll() is None)

    def close(self):
        self.closed = True
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    # ------------------------------------------------------------ processo
    def _command(self, enc, filters, opts):
        return [_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin",
                "-init_hw_device", f"d3d11va=dx:{self.adapter}", "-filter_hw_device", "dx",
                "-filter_complex",
                f"ddagrab=output_idx={self.output}:framerate={self.fps}:draw_mouse=0:dup_frames=0"
                + ("," + filters if filters else ""),
                "-fps_mode", "passthrough", "-c:v", enc, *opts, "-bf", "0", "-g", str(self.fps * 2),
                "-force_key_frames", "expr:gte(t,n_forced*1)",
                "-b:v", str(self.bitrate), "-maxrate", str(self.bitrate * 2),
                "-f", "flv", "-flvflags", "no_duration_filesize", "-flush_packets", "1", "-"]

    def _run(self):
        candidates = [e for e in ENCODERS if e[0] == FfmpegVideo.chosen] or ENCODERS
        for enc, filters, opts in candidates:
            if self.closed:
                return
            try:
                self.proc = subprocess.Popen(self._command(enc, filters, opts), stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                             creationflags=subprocess.CREATE_NO_WINDOW)
            except OSError as e:
                self.error = str(e)
                break
            _kill_with_parent(self.proc)
            self.encoder = enc
            got = self._read_flv(self.proc.stdout)
            if got or self.closed:
                FfmpegVideo.chosen = enc
                return
            err = self.proc.stderr.read().decode(errors="replace").strip()
            self.error = f"{enc}: {err[-300:] or 'terminato senza dati'}"
        if not self.closed:
            FfmpegVideo.all_failed = True
            self.closed = True

    def _read_flv(self, f):
        """Legge il flusso FLV; restituisce True se ha prodotto almeno un pacchetto."""
        def read(n):
            b = bytearray()
            while len(b) < n:
                chunk = f.read(n - len(b))
                if not chunk:
                    raise EOFError
                b += chunk
            return bytes(b)

        got = False
        sps = pps = None
        length_size = 4
        try:
            head = read(9)
            if head[:3] != b"FLV":
                return False
            read(int.from_bytes(head[5:9], "big") - 9 + 4)  # resto intestazione + PreviousTagSize0
            while True:
                th = read(11)
                ttype, size = th[0], int.from_bytes(th[1:4], "big")
                data = read(size)
                read(4)  # PreviousTagSize
                if ttype != 9 or len(data) < 5:
                    continue
                key = (data[0] >> 4) == 1
                ptype = data[1]
                body = data[5:]
                if ptype == 0:  # configurazione: SPS e PPS
                    if len(body) < 7:
                        continue  # Media Foundation: SPS/PPS arrivano dentro i fotogrammi
                    length_size = (body[4] & 3) + 1
                    n_sps = body[5] & 0x1F
                    i = 6
                    for _ in range(n_sps):
                        ln = struct.unpack(">H", body[i:i + 2])[0]
                        sps = body[i + 2:i + 2 + ln]
                        i += 2 + ln
                    n_pps = body[i]
                    i += 1
                    for _ in range(n_pps):
                        ln = struct.unpack(">H", body[i:i + 2])[0]
                        pps = body[i + 2:i + 2 + ln]
                        i += 2 + ln
                    continue
                if ptype != 1:
                    continue
                got = True
                annexb = _avcc_to_annexb(body, length_size)
                if key and sps and pps:
                    annexb = b"\x00\x00\x00\x01" + sps + b"\x00\x00\x00\x01" + pps + annexb
                with self.lock:
                    if not self.active or (self.need_key and not key):
                        continue
                    self.need_key = False
                    self.frames.append(b"V" + bytes([1 if key else 0]) + annexb)
                    if len(self.frames) > 120:   # client troppo lento: non accumulare
                        self.frames.clear()
                        self.need_key = True
                if self.on_frame:
                    self.on_frame()
        except (EOFError, OSError, ValueError, IndexError, struct.error):
            pass
        return got
