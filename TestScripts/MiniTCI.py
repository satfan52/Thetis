"""
MiniTCI — a small simplified SDR radio for Thetis headless TCI ports.

Connects to RX1..RX8 (ws://127.0.0.1:50001..50008). Live panadapter +
waterfall from the IQ stream, receiver audio out, band/mode/filter/VFO
control, S-meter, and PTT transmit using the PC microphone via TCI.

Requires: numpy, sounddevice, websockets, Pillow
  python3 -m pip install numpy sounddevice websockets pillow

Run:  python3 MiniTCI.py
"""

import asyncio
import collections
import queue
import struct
import threading
import tkinter as tk
from tkinter import ttk

import numpy as np
import sounddevice as sd
import websockets

try:
    from PIL import Image, ImageTk
    PIL_OK = True
except ImportError:
    PIL_OK = False

# ---------------------------------------------------------------- config

HOST = "127.0.0.1"
PORT_MIN, PORT_MAX = 50001, 50008
OUT_RATE = 48000
MIC_RATE = 48000

BANDS = [  # name, default MHz, suggested mode
    ("160m", 1.850, "LSB"),
    ("80m",  3.600, "LSB"),
    ("60m",  5.354, "USB"),
    ("40m",  7.060, "LSB"),
    ("30m", 10.116, "CWU"),
    ("20m", 14.074, "USB"),
    ("17m", 18.100, "USB"),
    ("15m", 21.074, "USB"),
    ("12m", 24.920, "USB"),
    ("10m", 28.400, "USB"),
    ("6m",  50.313, "USB"),
    ("2m", 144.200, "NFM"),
]

MODES = ["USB", "LSB", "DIGU", "DIGL", "CWU", "CWL", "AM", "SAM", "NFM"]
FILTERS = {
    "USB": (100, 2900), "LSB": (-2900, -100),
    "DIGU": (100, 3100), "DIGL": (-3100, -100),
    "CWU": (300, 800), "CWL": (-800, -300),
    "AM": (-4500, 4500), "SAM": (-4500, 4500), "NFM": (-3500, 3500),
}

C = {"bg": "#e8eaf0", "panel": "#f5f6f9", "fg": "#1a1f29", "dim": "#5a6474",
     "green": "#1a7f37", "tune": "#b45309", "red": "#c0392b", "grid": "#d0d5dd"}

CANVAS_W = 900
PAN_H = 160
WF_H = 220


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def list_output_devices():
    devs = []
    try:
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0:
                devs.append((i, d["name"], int(d["default_samplerate"])))
    except Exception:
        pass
    return devs


def list_input_devices():
    devs = []
    try:
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                devs.append((i, d["name"], int(d["max_input_channels"])))
    except Exception:
        pass
    return devs


def resample(x, new_len):
    """Linear resample 1-D array to new_len samples."""
    if new_len <= 0 or len(x) == 0:
        return np.zeros(max(0, new_len), dtype=np.float32)
    pos = np.linspace(0, len(x) - 1, new_len)
    i0 = pos.astype(np.int32)
    i1 = np.minimum(i0 + 1, len(x) - 1)
    frac = (pos - i0).astype(np.float32)
    return (x[i0] * (1 - frac) + x[i1] * frac).astype(np.float32)


# ================================================================ TCI client

class TciClient:
    """Owns the websocket; runs an asyncio loop on a daemon thread."""

    def __init__(self, port):
        self.port = port
        self.running = False
        self.loop = None
        self._oq = None          # asyncio queue, created on the ws thread
        self.on_text = None
        self.on_audio = None
        self.on_iq = None
        self.on_chrono = None
        self.on_state = None

    def start(self, on_text, on_audio, on_iq, on_state, on_chrono):
        self.on_text, self.on_audio = on_text, on_audio
        self.on_iq, self.on_state = on_iq, on_state
        self.on_chrono = on_chrono
        self.running = True
        threading.Thread(target=self._thread_main, daemon=True).start()

    def stop(self):
        self.running = False
        if self.loop and self._oq is not None:
            self.loop.call_soon_threadsafe(self._oq.put_nowait, "__close__")

    def send(self, cmd):
        if self.loop and self._oq is not None:
            self.loop.call_soon_threadsafe(self._oq.put_nowait, cmd)

    def send_binary(self, data):
        if self.loop and self._oq is not None:
            self.loop.call_soon_threadsafe(self._oq.put_nowait, data)

    # ---- ws thread ----
    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._run())

    async def _run(self):
        import websockets
        uri = f"ws://{HOST}:{self.port}/"
        try:
            async with websockets.connect(
                    uri, max_size=None,
                    ping_interval=10, ping_timeout=20,
                    close_timeout=5) as ws:
                self._oq = asyncio.Queue()
                self.on_state("connected")
                await asyncio.gather(self._reader(ws), self._writer(ws))
        except Exception as e:
            self.on_text({"__error__": str(e)})
        finally:
            self.on_state("disconnected")

    async def _reader(self, ws):
        try:
            async for msg in ws:
                self._dispatch(msg)
        except Exception as e:
            self.on_text({"__error__": f"reader: {type(e).__name__}: {e}"})

    def _dispatch(self, msg):
        if isinstance(msg, str):
            for cmd in msg.split(";"):
                cmd = cmd.strip()
                if not cmd:
                    continue
                if ":" in cmd:
                    k, v = cmd.split(":", 1)
                    self.on_text({k.strip().lower(): v.strip()})
                else:
                    self.on_text({cmd.strip().lower(): ""})
        elif isinstance(msg, bytes) and len(msg) >= 64:
            ftype = struct.unpack("<I", msg[24:28])[0]
            rate = struct.unpack("<I", msg[4:8])[0]
            chans = struct.unpack("<I", msg[28:32])[0]
            length = struct.unpack("<i", msg[20:24])[0]
            data = np.frombuffer(msg[64:], dtype="<f4")
            if ftype == 1:                     # RX audio
                n = min(length, len(data))
                if chans == 2:
                    n -= n % 2
                self.on_audio(data[:n], rate, chans)
            elif ftype == 0 and len(data) >= 16:   # IQ
                n = min(length, len(data)); n -= n % 2
                self.on_iq(data[:n], rate)
            elif ftype == 3:                       # TX chrono
                self.on_chrono(length, rate)

    async def _writer(self, ws):
        while True:
            cmd = await self._oq.get()
            try:
                if cmd == "__close__":
                    await ws.close()
                    return
                await ws.send(cmd)
            except Exception:
                return


# ================================================================ panadapter

class PanFall(tk.Canvas):
    """Spectrum (top PAN_H px) + scrolling waterfall (below)."""

    DB_TOP = 5.0
    DB_BOT = -115.0

    def __init__(self, master):
        super().__init__(master, width=CANVAS_W, height=PAN_H + WF_H,
                         bg="#0a0f16", highlightthickness=0)
        self.span = 96000.0
        self.center_hz = 0.0
        self.vfo_hz = 0.0
        self.filt = (100, 2900)
        self._wf_img = np.zeros((WF_H, CANVAS_W, 3), dtype=np.uint8)
        self._photo = None
        self._col = None
        self._pil = PIL_OK

    def f2x(self, f):
        return (f - self.center_hz) / self.span * CANVAS_W + CANVAS_W / 2

    def x2f(self, x):
        return (x - CANVAS_W / 2) / CANVAS_W * self.span + self.center_hz

    def update(self, iq):
        n = len(iq) // 2
        if n < 32:
            return
        z = iq[0:2 * n:2].astype(np.float32) + 1j * iq[1:2 * n:2].astype(np.float32)
        w = np.hanning(n).astype(np.float32)
        spec = np.fft.fftshift(np.fft.fft(z * w))
        db = (20 * np.log10(np.abs(spec) / n + 1e-10)).astype(np.float32)
        # Branch G S-meter: signal dBFS from IQ power (AGC-flattened audio RMS is
        # useless as a meter - it pins at the AGC target). Report peak bin level.
        self.peak_dbfs = float(db.max()) if len(db) else -140.0

        col = np.full(CANVAS_W, self.DB_BOT - 20, dtype=np.float32)
        idx = ((np.arange(len(db)) - len(db) / 2) / len(db) * CANVAS_W
               + CANVAS_W / 2).astype(np.int32)
        idx = np.clip(idx, 0, CANVAS_W - 1)
        np.maximum.at(col, idx, db)
        self._col = col

        # adaptive contrast: track the noise floor and stretch the display
        # range around it (like Thetis does) so weak signals stay visible
        floor = float(np.percentile(col, 30))
        self._floor = 0.85 * getattr(self, "_floor", floor) + 0.15 * floor
        top = floor + 55.0   # 55 dB of dynamic range above the floor
        lo, hi = self._floor, top
        norm = np.clip((col - lo) / (hi - lo), 0, 1)
        self._norm_lo, self._norm_hi = lo, hi

        self._wf_img = np.roll(self._wf_img, 1, axis=0)   # newest at top
        self._wf_img[0] = self._cmap(norm)[::-1]           # low freq left
        self._draw()

    # Thetis-style high-contrast palette: black -> deep blue -> cyan ->
    # green -> yellow -> red -> white (like the Thetis waterfall)
    _CMAP_CACHE = None

    @classmethod
    def _build_cmap(cls):
        if cls._CMAP_CACHE is not None:
            return cls._CMAP_CACHE
        stops = [
            (0.00, (0, 0, 0)),
            (0.10, (0, 0, 64)),
            (0.25, (0, 40, 160)),
            (0.40, (0, 160, 220)),
            (0.55, (0, 210, 120)),
            (0.70, (230, 230, 60)),
            (0.75, (255, 160, 30)),
            (0.85, (255, 70, 30)),
            (1.00, (255, 255, 255)),
        ]
        lut = np.zeros((256, 3), dtype=np.uint8)
        for i in range(len(stops) - 1):
            p0, c0 = stops[i]
            p1, c1 = stops[i + 1]
            n0, n1 = int(p0 * 255), int(p1 * 255)
            if n1 <= n0:
                continue
            t = np.linspace(0, 1, max(1, n1 - n0 + 1))[:, None]
            lut[n0:n1 + 1] = (np.array(c0) * (1 - t) + np.array(c1) * t)
        lut = np.clip(lut, 0, 255).astype(np.uint8)
        cls._CMAP_CACHE = lut
        return lut

    @classmethod
    def _cmap(cls, v):
        lut = cls._build_cmap()
        idx = np.clip((v * 255).astype(np.int32), 0, 255)
        return lut[idx]

    def _draw(self):
        col = getattr(self, "_col", None)
        if col is None:
            return
        pan = np.zeros((PAN_H, CANVAS_W, 3), dtype=np.uint8)
        pan[:] = (10, 12, 18)
        lo = getattr(self, "_norm_lo", self.DB_BOT)
        hi = getattr(self, "_norm_hi", self.DB_TOP)
        ys = (PAN_H - 1 - np.clip(
            (col - lo) / max(1e-6, hi - lo) * (PAN_H - 1),
            0, PAN_H - 1)).astype(np.int32)
        # 3px bright green trace (Thetis-style)
        trace = np.array([70, 240, 100], dtype=np.uint8)
        for dy in (-1, 0, 1):
            yy = np.clip(ys + dy, 0, PAN_H - 1)
            pan[yy, np.arange(CANVAS_W)] = trace

        if self._pil:
            self._photo = ImageTk.PhotoImage(Image.fromarray(
                np.vstack([pan, self._wf_img])))
            self.delete("all")
            self.create_image(0, 0, image=self._photo, anchor="nw")
        else:
            self.delete("all")
            pts = []
            for x in range(0, CANVAS_W, 3):
                y = ys[x]
                pts += [x, y]
            self.create_line(pts, fill=C["green"], width=1)

        # grid
        for i in range(1, 4):
            y = i * PAN_H / 4
            self.create_line(0, y, CANVAS_W, y, fill=C["grid"])

        # labels
        for k in range(-4, 5):
            off = k * self.span / 8
            x = self.f2x(self.center_hz + off)
            if 14 <= x <= CANVAS_W - 14:
                self.create_text(x, PAN_H + 10,
                                 text=f"{off / 1000:+.0f}k",
                                 fill="#9aa4b2", font=("Segoe UI", 7))

        # filter overlay (relative to VFO)
        if self.center_hz and self.vfo_hz:
            x1 = self.f2x(self.vfo_hz + self.filt[0])
            x2 = self.f2x(self.vfo_hz + self.filt[1])
            if x2 > x1 and x2 > 0 and x1 < CANVAS_W:
                self.create_rectangle(x1, 0, x2, PAN_H, fill="", outline=C["tune"])

        # vfo line
        if self.center_hz:
            x = self.f2x(self.vfo_hz)
            if 0 <= x <= CANVAS_W:
                self.create_line(x, 0, x, PAN_H + WF_H, fill=C["tune"], width=1)


# ================================================================ app

class MiniTCI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MiniTCI — simplified Thetis radio")
        self.configure(bg="#cfd4dd")
        self.geometry("950x660")
        self.minsize(920, 620)

        self.client = None
        self.connected = False
        self.ptt = False
        self.freq_hz = 14_074_000
        self.mode = "USB"
        self.volume = 0.25
        self.mic_gain = 0.5
        self.smeter = -140.0
        self.mic_stream = None
        self.tx_audio_q = collections.deque(maxlen=64)
        self.chrono_reqs = collections.deque()
        self.chrono_lock = threading.Lock()
        self.tx_pos = 0

        self.audio_blocks = collections.deque()
        self.audio_pos = 0
        self.out_stream = None
        self._iq_q = queue.Queue(maxsize=8)
        self.text_q = queue.Queue()
        self.iq_q = queue.Queue(maxsize=4)

        self._build_ui()
        self._open_output()
        self.after(50, self._poll)

    # ---------------- build UI ----------------
    def _build_ui(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("TFrame", background=C["panel"])
        s.configure("TLabel", background=C["panel"], foreground=C["fg"])
        s.configure("TButton", background="#dfe3ea", foreground="#111111")
        s.configure("TCombobox", fieldbackground="#ffffff", background=C["panel"],
                    foreground="#000000", arrowcolor="#1a1f29")
        s.configure("TCombobox.Listbox", fieldbackground="#ffffff",
                    background="#ffffff", foreground="#111111")
        self.option_add("*TCombobox*Listbox.background", "#ffffff")
        self.option_add("*TCombobox*Listbox.foreground", "#111111")
        self.option_add("*TCombobox*Listbox.selectBackground", "#cce4ff")
        self.option_add("*TCombobox*Listbox.selectForeground", "#000000")
        s.map("TButton", background=[("active", "#c8cfda")])

        # --- row 1: connection + band + mode
        r1 = ttk.Frame(self); r1.pack(fill="x", padx=10, pady=(8, 2))
        ttk.Label(r1, text="Receiver:").pack(side="left")
        self.rx_var = tk.StringVar(value="50003 (RX3)")
        ttk.Combobox(r1, textvariable=self.rx_var, width=11, state="readonly",
                     values=[f"{p} (RX{p - 50000})" for p in range(PORT_MIN, PORT_MAX + 1)]
                     ).pack(side="left", padx=(4, 10))
        self.conn_btn = ttk.Button(r1, text="Connect", width=11, command=self.toggle_conn)
        self.conn_btn.pack(side="left")
        ttk.Label(r1, text="Band:", padding=(14, 0, 2, 0)).pack(side="left")
        self.band_var = tk.StringVar(value="20m")
        ttk.Combobox(r1, textvariable=self.band_var, width=5, state="readonly",
                     values=[b[0] for b in BANDS]).pack(side="left")
        self.band_var.trace_add("write", self._band_changed)
        ttk.Label(r1, text="Mode:", padding=(14, 0, 2, 0)).pack(side="left")
        self.mode_var = tk.StringVar(value="USB")
        ttk.Combobox(r1, textvariable=self.mode_var, width=5, state="readonly",
                     values=MODES).pack(side="left")
        self.mode_var.trace_add("write", self._mode_changed)

        ttk.Label(r1, text="AGC:", padding=(14, 0, 2, 0)).pack(side="left")
        self.agc_var = tk.StringVar(value="MED")
        self.agc_box = ttk.Combobox(r1, textvariable=self.agc_var, width=8, state="readonly",
                                    values=["OFF", "FAST", "MED", "SLOW", "LONG", "CUSTOM"])
        self.agc_box.pack(side="left")
        self.agc_var.trace_add("write", self._agc_changed)
        self.agc_auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r1, text="Auto", variable=self.agc_auto_var,
                        command=self._agc_auto_changed).pack(side="left", padx=(6, 2))
        ttk.Label(r1, text="Gain:", padding=(8, 0, 2, 0)).pack(side="left")
        self.agc_gain_var = tk.DoubleVar(value=40)
        ttk.Scale(r1, from_=-20, to=120, variable=self.agc_gain_var, length=90,
                  command=self._agc_gain_changed).pack(side="left", padx=2)

        # --- row 2: frequency
        r2 = ttk.Frame(self); r2.pack(fill="x", padx=10, pady=2)
        self.freq_lbl = tk.Label(r2, text="14.074.000 kHz", bg=C["panel"], fg=C["tune"],
                                 font=("Consolas", 24, "bold"))
        self.freq_lbl.pack(side="left", padx=(2, 14))
        for txt, hz in (("−1k", -1000), ("−100", -100), ("+100", 100), ("+1k", 1000)):
            ttk.Button(r2, text=txt, width=4,
                       command=lambda d=hz: self.tune_to(self.freq_hz + d)
                       ).pack(side="left", padx=2)
        ttk.Label(r2, text="Direct kHz:", padding=(12, 0, 2, 0)).pack(side="left")
        self.tune_entry = ttk.Entry(r2, width=10)
        self.tune_entry.pack(side="left")
        ttk.Button(r2, text="Go", width=4, command=self._tune_direct).pack(side="left", padx=4)

        # --- panadapter + waterfall
        self.pan = PanFall(self)
        self.pan.pack(fill="both", expand=True, padx=10, pady=4)
        self.pan.bind("<ButtonRelease-1>", self._pan_release)

        # --- row 3: volume + sound devices + smeter
        r3 = ttk.Frame(self); r3.pack(fill="x", padx=10, pady=2)
        ttk.Label(r3, text="Volume:").pack(side="left")
        self.vol_var = tk.DoubleVar(value=70)
        ttk.Scale(r3, from_=0, to=100, variable=self.vol_var, length=140,
                  command=self._vol_changed).pack(side="left", padx=4)
        self.volume = 0.7 * 2.4

        self._out_devs = list_output_devices()
        self._in_devs = list_input_devices()
        ttk.Label(r3, text="Speaker:", padding=(10, 0, 2, 0)).pack(side="left")
        self.out_dev_var = tk.StringVar(value="(system default)")
        out_names = ["(system default)"] + [n for _, n, _ in self._out_devs]
        ttk.Combobox(r3, textvariable=self.out_dev_var, width=22, state="readonly",
                     values=out_names).pack(side="left", padx=2)
        self.out_dev_var.trace_add("write", lambda *_: self._reopen_output())

        self.sm = tk.Canvas(r3, width=210, height=26, bg=C["panel"], highlightthickness=0)
        self.sm.pack(side="left", padx=20)
        self.sm_bar = self.sm.create_rectangle(2, 6, 2, 22, fill=C["green"], width=0)
        self.sm_txt = self.sm.create_text(206, 14, text="−140 dBFS", anchor="e",
                                          fill=C["fg"], font=("Consolas", 9))
        self.state_lbl = tk.Label(r3, text="● disconnected", bg=C["panel"], fg=C["dim"],
                                  font=("Segoe UI", 9))
        self.state_lbl.pack(side="right")

        # --- row 4: TX
        r4 = ttk.Frame(self); r4.pack(fill="x", padx=10, pady=4)
        self.ptt_btn = tk.Button(r4, text="PTT", bg="#f4d7d4", fg=C["fg"], width=8,
                                 font=("Segoe UI", 10, "bold"))
        self.ptt_btn.bind("<ButtonPress-1>", lambda e: self.ptt_on())
        self.ptt_btn.bind("<ButtonRelease-1>", lambda e: self.ptt_off())
        self.ptt_btn.pack(side="left")
        self.bind("<KeyPress-space>", self._space_dn)
        self.bind("<KeyRelease-space>", self._space_up)
        self.tx_lbl = tk.Label(r4, text="RX", bg=C["panel"], fg=C["dim"],
                               font=("Segoe UI", 11, "bold"))
        self.tx_lbl.pack(side="left", padx=12)
        ttk.Label(r4, text="Mic:", padding=(10, 0, 2, 0)).pack(side="left")
        self._in_devs = list_input_devices()
        self.in_dev_var = tk.StringVar(value="(system default)")
        in_names = ["(system default)"] + [n for _, n, _ in self._in_devs]
        ttk.Combobox(r4, textvariable=self.in_dev_var, width=22, state="readonly",
                     values=in_names).pack(side="left", padx=2)
        self.in_dev_var.trace_add("write", lambda *_: self._reopen_input())
        ttk.Label(r4, text="Mic gain:").pack(side="left")
        self.mic_var = tk.DoubleVar(value=50)
        ttk.Scale(r4, from_=0, to=100, variable=self.mic_var, length=120,
                  command=self._mic_changed).pack(side="left", padx=4)
        self.mic_gain = 0.5

        # --- log
        self.log = tk.Text(self, height=5, bg="#ffffff", fg="#333333", borderwidth=1,
                           font=("Consolas", 8))
        self.log.pack(fill="x", padx=10, pady=(2, 8))

    # ---------------- audio out ----------------
    def _open_output(self):
        try:
            dev = self._find_dev(self._out_devs, self.out_dev_var.get()) \
                if hasattr(self, "out_dev_var") else None
            kwargs = dict(samplerate=OUT_RATE, channels=2, dtype="float32",
                          blocksize=1024, callback=self._out_cb)
            if dev is not None:
                kwargs = {"device": dev, "samplerate": OUT_RATE, "channels": 2,
                          "dtype": "float32", "blocksize": 1024,
                          "callback": self._out_cb}
            self.out_stream = sd.OutputStream(**kwargs)
            self.out_stream.start()
            self.logprint(f"output device: {'default' if dev is None else self.out_dev_var.get()}")
        except Exception as e:
            self.out_stream = None
            self.logprint(f"audio out error: {e}")

    @staticmethod
    def _find_dev(devs, name):
        if not name or name.startswith("("):
            return None
        for i, n, _ in devs:
            if n == name:
                return i
        return None

    def _reopen_output(self):
        if self.out_stream:
            try:
                self.out_stream.stop(); self.out_stream.close()
            except Exception:
                pass
            self.out_stream = None
        self._open_output()

    def _out_cb(self, outdata, frames, t, status):
        filled = 0
        while filled < frames and self.audio_blocks:
            blk = self.audio_blocks[0]
            take = min(len(blk) - self.audio_pos, frames - filled)
            seg = blk[self.audio_pos:self.audio_pos + take]
            outdata[filled:filled + take, 0] = seg
            filled += take
            self.audio_pos += take
            if self.audio_pos >= len(blk):
                self.audio_blocks.popleft()
                self.audio_pos = 0
        if filled < frames:
            outdata[filled:, 0] = 0.0
        outdata[:, 0] *= self.volume
        outdata[:, 1] = outdata[:, 0]

    def _vol_changed(self, v):
        # makeup gain: server ships -26 dB calibrated audio; +6 dB over previous
        # mapping (1.2 -> 2.4) so quiet signals are clearly audible
        self.volume = (float(v) / 100.0) * 2.4

    def _mic_changed(self, v):
        self.mic_gain = float(v) / 100.0

    # ---------------- TCI callbacks (ws thread) ----------------
    def tci_text(self, d):
        self.text_q.put(d)

    def tci_audio(self, data, rate, chans):
        # mono-ize then resample to OUT_RATE. Server sends 4096-value blocks
        # (2048 stereo samples = 85ms); the old 40-block queue allowed >3s of
        # latency. Keep at most ~4 blocks (~340ms) so playback stays near-live
        # and drop-oldest never yanks the read pointer mid-block.
        if chans == 2:
            mono = data[0::2].copy()
        else:
            mono = data
        if rate != OUT_RATE:
            mono = resample(mono, int(len(mono) * OUT_RATE / rate))
        self.audio_blocks.append(mono)
        while len(self.audio_blocks) > 5:       # ~0.34 s hard cap = low latency
            self.audio_blocks.popleft()
            self.audio_pos = 0

    def tci_iq(self, data, rate):
        # Server streams tiny IQ frames (hundreds of samples); accumulate into
        # larger FFT blocks for a defined panadapter/waterfall.
        try:
            self._iq_acc = np.concatenate((self._iq_acc, data)) \
                if getattr(self, "_iq_acc", None) is not None else data.copy()
            self._iq_acc_rate = rate
            target = 8192  # interleaved values (4096 complex)
            while len(self._iq_acc) >= target:
                block = self._iq_acc[:target]
                self._iq_acc = self._iq_acc[target:]
                self._iq_q.put_nowait((block, rate))
        except Exception:
            pass

    def tci_state(self, s):
        self.text_q.put({"__state__": s})

    def tci_chrono(self, length, rate):
        # Server paces TX audio: each chrono requests `length` interleaved values.
        with self.chrono_lock:
            self.chrono_reqs.append((length, rate))

    # ---- TX audio frame builder (WSJT-X style) ----
    def build_tx_audio_frame(self, mono, rate, chans):
        """Wrap mono float32 samples into a 64-byte-header TX_AUDIO_STREAM frame."""
        n = len(mono)
        if chans == 2:
            payload_vals = n * 2
            body = np.empty(n * 2, dtype="<f4")
            body[0::2] = mono
            body[1::2] = mono
        else:
            payload_vals = n
            body = mono
        words = [0] * 16                # 16 x uint32 = 64-byte TCI header
        words[0] = 0                    # receiver (TRX 0)
        words[1] = int(rate)
        words[2] = 3                    # TCISampleType.FLOAT32
        words[5] = int(payload_vals)    # length (interleaved value count)
        words[6] = 2                    # TCIStreamType.TX_AUDIO_STREAM
        words[7] = int(chans)
        hdr = struct.pack("<16I", *words)
        return hdr + body.astype("<f4").tobytes()

    def service_tx_audio(self):
        """Send mic audio blocks in response to chrono requests (called from UI poll)."""
        if not self.ptt or not self.connected or not self.client:
            with self.chrono_lock:
                self.chrono_reqs.clear()
            return
        while self.chrono_reqs:
            with self.chrono_lock:
                if not self.chrono_reqs:
                    break
                length, rate = self.chrono_reqs.popleft()
            chans = 1
            vals_needed = max(1, length) if chans == 1 else max(1, length // 2)
            # gather mic samples
            mono = bytearray()
            got = 0
            while got < vals_needed and self.tx_audio_q:
                blk = self.tx_audio_q[0]
                take = min(len(blk) - self.tx_pos, vals_needed - got)
                mono += blk[self.tx_pos:self.tx_pos + take].tobytes()
                got += take
                self.tx_pos += take
                if self.tx_pos >= len(blk):
                    self.tx_audio_q.popleft()
                    self.tx_pos = 0
            if got == 0:
                continue
            vals = np.frombuffer(bytes(mono), dtype=np.float32)[:got]
            vals = np.clip(vals * self.mic_gain * 2.0, -1.0, 1.0)
            frame = self.build_tx_audio_frame(vals, rate, chans)
            if self.client and self.client.loop:
                self.client.send_binary(frame)

    # ---------------- connect ----------------
    def toggle_conn(self):
        if self.client:
            self.client.send("__close__")
            self.client = None
            self._set_state("disconnected")
            return
        port = int(self.rx_var.get().split()[0])
        self.text_q = queue.Queue()
        self._iq_q = queue.Queue(maxsize=8)
        self.chrono_reqs = collections.deque()
        self.tx_audio_q = collections.deque(maxlen=64)
        self.tx_pos = 0
        c = TciClient(port)
        self.client = c
        self._set_state("connecting")      # set UI state BEFORE the thread can race us
        c.start(self.tci_text, self.tci_audio, self.tci_iq, self.tci_state,
                self.tci_chrono)

    def _set_state(self, s):
        if s == "connected":
            self.connected = True
            self.conn_btn.config(text="Disconnect")
            self.state_lbl.config(text="● connected", fg=C["green"])
            self.send("iq_samplerate:96000;")
            self.send("iq_start:0;")
            self.send("audio_start:0;")
            self.send("rx_sensors_enable:true,250;")
            self.send(f"vfo:0,0,{self.freq_hz};")
            self.send(f"modulation:0,{self.mode};")
            lo, hi = FILTERS.get(self.mode, (100, 2900))
            self.send(f"rx_filter_band:0,{lo},{hi};")
            self.send(f"agc_mode:0,{self._agc_mode_to_tci(self.agc_var.get())};")
            self.send(f"agc_auto_ex:0,{str(self.agc_auto_var.get()).lower()};")
            self.send(f"agc_gain:0,{int(self.agc_gain_var.get())};")
        elif s == "connecting":
            self.conn_btn.config(text="Cancel")
            self.state_lbl.config(text="● connecting…", fg=C["tune"])
        else:
            was = self.connected
            self.connected = False
            self.conn_btn.config(text="Connect")
            self.state_lbl.config(text="● disconnected", fg=C["dim"])
            if was:
                self.logprint("connection lost - click Connect to reconnect")
            else:
                self.logprint("disconnected")

    # ---------------- poll queue ----------------
    def _poll(self):
        try:
            for _ in range(300):
                item = self.text_q.get_nowait()
                if "__state__" in item:
                    self._set_state(item["__state__"])
                elif "__error__" in item:
                    self.logprint("error: " + str(item["__error__"]))
                    self._set_state("disconnected")
                else:
                    self._handle(item)
        except queue.Empty:
            pass

        try:
            data, rate = self._iq_q.get_nowait()
            if rate and int(rate) != int(self.pan.span):
                self.pan.span = float(rate)
            self.pan.update(data)
        except (queue.Empty, AttributeError):
            pass

        self.service_tx_audio()
        self._draw_smeter()
        self.after(50, self._poll)

    def _handle(self, d):
        for k, v in d.items():
            if k == "vfo" and v:
                p = v.split(",")
                if len(p) >= 3:
                    try:
                        hz = float(p[2])
                        self.freq_hz = int(hz)
                        self._fmt_freq()
                        self.pan.vfo = hz
                        if not self.pan.center_hz:
                            self.pan.center_hz = hz
                    except ValueError:
                        pass
            elif k == "dds" and v:
                try:
                    self.pan.center_hz = float(v.split(",")[-1])
                except (ValueError, IndexError):
                    pass
            elif k == "trx" and v:
                tx = v.split(",")[-1].lower() == "true"
                if tx != self.ptt:
                    self.ptt = tx
                    self.tx_lbl.config(text="TX ⏺" if tx else "RX",
                                       fg=C["red"] if tx else C["dim"])
            elif k == "rx_sensors" and v:
                try:
                    self.smeter = float(v.split(",")[-1])
                except ValueError:
                    pass
            elif k == "rx_filter_band" and v:
                p = v.split(",")
                if len(p) >= 3:
                    try:
                        self.pan.filt = (float(p[1]), float(p[2]))
                    except ValueError:
                        pass
            elif k == "iq_samplerate" and v:
                try:
                    self.pan.span = float(int(v))
                except ValueError:
                    pass

    def _fmt_freq(self):
        khz = int(self.freq_hz / 1000)
        self.freq_lbl.config(text=f"{khz:,} kHz".replace(",", "."))

    def _draw_smeter(self):
        # prefer IQ-derived level (peak bin dBFS); rx_sensors (audio RMS) is
        # AGC-flattened and pins at the AGC target, which is useless.
        db = getattr(self.pan, "peak_dbfs", None)
        if db is None:
            db = self.smeter
        self.smeter_db = db
        # scale: -140..0 dBFS with S-points approx: S9 ~ -73 dBFS region
        frac = clamp((db + 140.0) / 140.0, 0, 1)
        w = int(202 * frac)
        self.sm.coords(self.sm_bar, 2, 6, 2 + w, 22)
        if db > -20:
            color = "#c0392b"
        elif db > -40:
            color = "#b45309"
        else:
            color = "#1a7f37"
        self.sm.itemconfig(self.sm_bar, fill=color)
        self.sm.itemconfig(self.sm_txt, text=f"{db:.0f} dBFS")

    # ---------------- controls ----------------
    def send(self, cmd):
        if self.client and self.client.loop:
            self.client.send(cmd)

    def nudge(self, hz):
        self.tune_to(self.freq_hz + hz)

    def tune_to(self, hz):
        self.freq_hz = int(hz)
        self._fmt_freq()
        self.pan.vfo = self.freq_hz
        if not self.pan.center_hz:
            self.pan.center_hz = self.freq_hz
        self.send(f"vfo:0,0,{self.freq_hz};")

    def _tune_direct(self):
        t = self.tune_entry.get().strip().replace(",", ".")
        try:
            mhz = float(t) if "." in t else float(t) / 1000.0
            self.tune_to(int(mhz * 1e6))
        except ValueError:
            self.logprint("bad frequency (use MHz like 14.074, or kHz)")

    def _band_changed(self, *_):
        name = self.band_var.get()
        for b in BANDS:
            if b[0] == name:
                self.tune_to(int(b[1] * 1e6))
                self.mode_var.set(b[2])
                return

    def _mode_changed(self, *_):
        self.mode = self.mode_var.get()
        lo, hi = FILTERS.get(self.mode, (100, 2900))
        self.pan.filt = (lo, hi)
        self.send(f"modulation:0,{self.mode};")
        self.send(f"rx_filter_band:0,{lo},{hi};")

    def _agc_mode_to_tci(self, name):
        return {"OFF": "off", "FIXED": "fixed", "FAST": "fast",
                "MED": "normal", "SLOW": "slow", "LONG": "long",
                "CUSTOM": "custom"}.get(name, "normal")

    def _agc_changed(self, *_):
        if not self.connected:
            return
        mode = self.agc_var.get()
        self.send(f"agc_mode:0,{self._agc_mode_to_tci(mode)};")
        # OFF -> switch to manual gain (agc_auto false) and push gain
        if mode == "OFF":
            self.agc_auto_var.set(False)
            self.send("agc_auto_ex:0,false;")
            self.send(f"agc_gain:0,{int(self.agc_gain_var.get())};")
        else:
            self.agc_auto_var.set(True)
            self.send("agc_auto_ex:0,true;")

    def _agc_auto_changed(self):
        if not self.connected:
            return
        auto = self.agc_auto_var.get()
        self.send(f"agc_auto_ex:0,{str(auto).lower()};")
        if not auto:
            self.send(f"agc_gain:0,{int(self.agc_gain_var.get())};")
            if self.agc_var.get() != "OFF":
                self.agc_var.set("OFF")

    def _agc_gain_changed(self, v):
        if self.connected and not self.agc_auto_var.get():
            self.send(f"agc_gain:0,{int(float(v))};")

    def _pan_click(self, e):
        # tune only on final click; drags suppressed - flooding vfo commands
        # per mouse-move pixel can drop the connection
        pass

    def _pan_release(self, e):
        if self.pan.center_hz:
            f = self.pan.x2f(e.x)
            self.tune_to(int(round(f / 10.0)) * 10)   # 100 Hz grid

    # ---------------- TX ----------------
    def ptt_on(self):
        if not self.connected or self.ptt:
            return
        self.ptt = True
        self.send("tx_stream_audio_buffering:100;")
        self.send("audio_stream_sample_type:float32;")
        self.send("audio_stream_channels:1;")
        self.send("audio_stream_samples:1024;")
        self.send("trx:0,true;")
        self.ptt_btn.config(bg=C["red"], relief="sunken")
        self.tx_lbl.config(text="TX ⏺", fg=C["red"])
        self._mic_open()

    def ptt_off(self):
        if not self.ptt:
            return
        self.ptt = False
        self.send("trx:0,false;")
        self.ptt_btn.config(bg="#e3b8b3", relief="raised")
        self.tx_lbl.config(text="RX", fg=C["dim"])
        if self.mic_stream:
            try:
                self.mic_stream.stop(); self.mic_stream.close()
            except Exception:
                pass
            self.mic_stream = None

    def _space_dn(self, e):
        self.ptt_on()

    def _space_up(self, e):
        self.ptt_off()

    def _mic_open(self):
        if self.mic_stream:
            return
        try:
            kw = {"samplerate": MIC_RATE, "channels": 1, "dtype": "float32",
                  "blocksize": 1024, "callback": self._mic_cb}
            dev = self._find_dev(self._in_devs, self.in_dev_var.get())
            if dev is not None:
                kw["device"] = dev
            self.mic_stream = sd.InputStream(**kw)
            self.mic_stream.start()
            name = self.in_dev_var.get() if dev is not None else "system default"
            self.logprint(f"mic open: {name}")
        except Exception as e:
            self.logprint(f"mic error: {e}")

    def _reopen_input(self):
        if self.ptt:
            self._mic_open()

    def _mic_cb(self, indata, frames, t, status):
        if self.ptt and self.connected:
            self.tx_audio_q.append(indata.reshape(-1).copy())
            self.tx_pos = 0

    # ---------------- log ----------------
    def logprint(self, s):
        try:
            self.log.insert("end", s + "\n")
            self.log.see("end")
            if float(self.log.index("end-1c").split(".")[0]) > 60:
                self.log.delete("1.0", "20.0")
        except Exception:
            pass


if __name__ == "__main__":
    MiniTCI().mainloop()