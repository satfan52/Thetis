"""
MiniTCI — a small simplified SDR radio for Thetis headless TCI ports.

Connects to RX1..RX8 (ws://127.0.0.1:50001..50008). Live panadapter +
waterfall from the IQ stream, receiver audio out, band/mode/filter/VFO
control, S-meter, and PTT transmit using the PC microphone via TCI.

Requires: numpy, sounddevice, websockets, Pillow
  python3 -m pip install numpy sounddevice websockets pillow

Run:  python3 MiniTCI.py
"""

import collections
import queue
import struct
import threading
import tkinter as tk
from tkinter import ttk

import numpy as np
import sounddevice as sd
import websockets

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

C = {"bg": "#0d1117", "panel": "#161b22", "fg": "#dfe7f3", "dim": "#7d8aa0",
     "green": "#39d353", "tune": "#ffb02e", "red": "#ff5555", "grid": "#262d38"}

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
            async with websockets.connect(uri, max_size=None) as ws:
                self._oq = asyncio.Queue()
                self.on_state("connected")
                await asyncio.gather(self._reader(ws), self._writer(ws))
        except Exception as e:
            self.on_text({"__error__": str(e)})
        finally:
            self.on_state("disconnected")

    async def _reader(self, ws):
        async for msg in ws:
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
            elif isinstance(msg, bytes) and len(msg) >= 32:
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

    DB_TOP, DB_BOT = 5.0, -115.0

    def __init__(self, master):
        super().__init__(master, width=CANVAS_W, height=PAN_H + WF_H,
                         bg="#05070c", highlightthickness=0)
        self.span = 96000.0
        self.center_hz = 0.0
        self.vfo_hz = 0.0
        self.filt = (100, 2900)
        self.wf_img = np.zeros((WF_H, CANVAS_W, 3), dtype=np.uint8)
        self._photo = None
        self._col = None
        self._pil = True
        try:
            from PIL import Image, ImageTk  # noqa
        except ImportError:
            self._pil = False

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

        col = np.full(CANVAS_W, self.DB_BOT - 20, dtype=np.float32)
        idx = ((np.arange(len(db)) - len(db) / 2) / len(db) * CANVAS_W
               + CANVAS_W / 2).astype(np.int32)
        idx = np.clip(idx, 0, CANVAS_W - 1)
        np.maximum.at(col, idx, db)
        self._col = col

        norm = np.clip((col - self.DB_BOT) / (self.DB_TOP - self.DB_BOT), 0, 1)
        row = self._cmap(norm)
        self.wf_img = np.roll(self.wf_img, 1, axis=0)   # newest at TOP of waterfall
        self.wf_img[0] = row = self._cmap(norm)[::-1]   # low freq left
        self._draw()

    @staticmethod
    def _cmap(v):
        r = np.clip(v * 2.6 - 0.55, 0, 1)
        g = np.clip(v * 1.9 - 0.05, 0, 1)
        b = np.clip(v * 0.9 + 0.18, 0, 1)
        return (np.stack([r, g, b], axis=-1) * 255).astype(np.uint8)

    def _draw(self):
        col = getattr(self, "_col", None)
        if col is None:
            return
        pan = np.zeros((PAN_H, CANVAS_W, 3), dtype=np.uint8)
        pan[:] = (6, 9, 14)
        ys = (PAN_H - 1 - np.clip(
            (col - self.DB_BOT) / (self.DB_TOP - self.DB_BOT) * (PAN_H - 1),
            0, PAN_H - 1)).astype(np.int32)
        pan[ys, np.arange(CANVAS_W)] = (57, 211, 83)

        if self._pil:
            from PIL import Image, ImageTk
            self._photo = ImageTk.PhotoImage(Image.fromarray(
                np.vstack([pan, self.wf_img])))
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
                                 fill="#57606f", font=("Segoe UI", 7))

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
        self.configure(bg=C["bg"])
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
        s.configure("TButton", background=C["panel"], foreground=C["fg"])
        s.configure("TCombobox", fieldbackground=C["bg"], background=C["panel"],
                    foreground=C["fg"], arrowcolor=C["fg"])
        s.map("TButton", background=[("active", "#232c3a")])

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
        self.pan.bind("<Button-1>", self._pan_click)
        self.pan.bind("<B1-Motion>", self._pan_drag)

        # --- row 3: volume + sound devices + smeter
        r3 = ttk.Frame(self); r3.pack(fill="x", padx=10, pady=2)
        ttk.Label(r3, text="Volume:").pack(side="left")
        self.vol_var = tk.DoubleVar(value=30)
        ttk.Scale(r3, from_=0, to=100, variable=self.vol_var, length=140,
                  command=self._vol_changed).pack(side="left", padx=4)
        self.volume = 0.30

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
        self.ptt_btn = tk.Button(r4, text="PTT", bg="#37242a", fg=C["fg"], width=8,
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
        self.log = tk.Text(self, height=5, bg=C["bg"], fg=C["dim"], borderwidth=0,
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
        self.volume = float(v) / 100.0

    def _mic_changed(self, v):
        self.mic_gain = float(v) / 100.0

    # ---------------- TCI callbacks (ws thread) ----------------
    def tci_text(self, d):
        self.text_q.put(d)

    def tci_audio(self, data, rate, chans):
        # mono-ize then resample to OUT_RATE
        if chans == 2:
            mono = data[0::2].copy()
        else:
            mono = data
        if rate != OUT_RATE:
            mono = resample(mono, int(len(mono) * OUT_RATE / rate))
        self.audio_blocks.append(mono)
        while len(self.audio_blocks) > 40:      # ~0.85 s cap
            self.audio_blocks.popleft()
            self.audio_pos = 0

    def tci_iq(self, data, rate):
        try:
            self._iq_q.put_nowait((data, rate))
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
        c.start(self.tci_text, self.tci_audio, self.tci_iq, self.tci_state,
                self.tci_chrono)
        self.client = c
        self._set_state("connecting")

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
        elif s == "connecting":
            self.conn_btn.config(text="Cancel")
            self.state_lbl.config(text="● connecting…", fg=C["tune"])
        else:
            self.connected = False
            self.conn_btn.config(text="Connect")
            self.state_lbl.config(text="● disconnected", fg=C["dim"])
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
        frac = clamp((self.smeter + 140.0) / 140.0, 0, 1)
        w = int(202 * frac)
        self.sm.coords(self.sm_bar, 2, 6, 2 + w, 22)
        self.sm.itemconfig(self.sm_bar,
                           fill=C["red"] if self.smeter > -15 else C["green"])
        self.sm.itemconfig(self.sm_txt, text=f"{self.smeter:.0f} dBFS")

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

    def _pan_click(self, e):
        if self.pan.center_hz:
            f = self.pan.x2f(e.x)
            self.tune_to(int(round(f / 10.0)) * 10)   # 100 Hz grid

    def _pan_drag(self, e):
        self._pan_click(e)

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
        self.ptt_btn.config(bg="#37242a", relief="raised")
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