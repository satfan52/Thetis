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
import json
import os
import time
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
     "green": "#1a7f37", "tune": "#b45309", "red": "#c0392b", "grid": "#d0d5dd",
     "pan_bg": "#0a3d4a", "pan_grid": "#0a6a80", "pan_trace": "#c8e0f0",
     "pan_filt_lo": "#ff3030", "pan_filt_hi": "#ffd050"}

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
        self._oq = None
        self.last_frame_ts = 0.0
        self._streaming = False          # asyncio queue, created on the ws thread
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
                    ping_interval=None,        # lib keepalive disabled: its ping/pong
                                               # path deadlocks under our 750fps stream
                                               # (raw-socket client survives 90s+/108MB,
                                               # lib client dies at 45s - every time)
                    max_queue=8192,
                    close_timeout=5) as ws:
                self._oq = asyncio.Queue()
                self.on_state("connected")
                self.last_frame_ts = time.time()
                tasks = [asyncio.create_task(self._reader(ws)),
                         asyncio.create_task(self._writer(ws)),
                         asyncio.create_task(self._watchdog(ws))]
                done, pending = await asyncio.wait(tasks,
                    return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                await asyncio.sleep(0.05)
        except Exception as e:
            self.on_text({"__error__": str(e)})
        finally:
            self.on_state("disconnected")

    async def _reader(self, ws):
        try:
            async for msg in ws:
                self.last_frame_ts = time.time()
                self._dispatch(msg)
        except Exception as e:
            self.on_text({"__error__": f"reader: {type(e).__name__}: {e}"})

    async def _watchdog(self, ws):
        """Our own liveness check: streaming servers send constantly; if nothing
        arrives for 10s, the connection is dead - close it so the UI reconnects."""
        while True:
            await asyncio.sleep(2)
            if getattr(self, "last_frame_ts", 0) and \
               time.time() - self.last_frame_ts > 10.0 and \
               getattr(self, "_streaming", False):
                self.on_text({"__error__": "watchdog: no data for 10s - closing"})
                try:
                    await ws.close()
                except Exception:
                    pass
                return

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
        self.rate = 96000.0      # IQ data sample rate (from stream header)
        self.span = 96000.0      # display span in Hz (zoom) - independent of rate
        self.center_hz = 0.0     # display window center (absolute RF)
        self.data_center_hz = 0.0  # frequency the IQ data is centered on (=DDC/VFO)
        self.vfo_hz = 0.0
        self.filt = (100, 2900)
        self.y_zero = 0.0        # user offset in dB (Quisk graph_y_zero analogue)
        self.y_scale = 42.0      # dB of graph headroom above the floor
        self.wf_gamma = 1.0      # waterfall intensity (lower = brighter)
        self._wf_img = np.zeros((WF_H, CANVAS_W, 3), dtype=np.uint8)
        self._wf_img[:] = (0, 0, 0)  # Quisk: pure black waterfall background
        self._ready = None
        self._ready_ys = None
        self._photo = None
        self._col = None
        self._pil = PIL_OK

    def f2x(self, f):
        return (f - self.center_hz) / self.span * CANVAS_W + CANVAS_W / 2

    def x2f(self, x):
        return (x - CANVAS_W / 2) / CANVAS_W * self.span + self.center_hz

    def shift_waterfall(self, df_hz):
        """Slide the ENTIRE stored waterfall (past rows included) by the pixel
        delta of a frequency change, like Thetis: when the VFO moves +df, every
        row's content moves -df*px, so past data stays coherent with the axis.
        The exposed edge is filled by repeating the last column (smear) until
        real data replaces it."""
        # accumulate fractional pixels so slow tuning (50 Hz wheel steps < 1 px
        # at 96k span) still slides the history smoothly instead of jumping
        self._wf_shift_acc = getattr(self, "_wf_shift_acc", 0.0) \
            + (-df_hz / max(1.0, self.span) * CANVAS_W)
        px = int(self._wf_shift_acc)
        self._wf_shift_acc -= px
        if px == 0 or abs(px) >= CANVAS_W:
            if abs(px) >= CANVAS_W:
                self._wf_img[:] = 0
            return
        wf = self._wf_img
        if px > 0:
            edge = wf[:, :1]          # leftmost column repeats into the void
            wf[:, px:] = wf[:, :-px]
            wf[:, :px] = edge
        else:
            edge = wf[:, -1:]
            wf[:, :px] = wf[:, -px:]
            wf[:, px:] = edge

    def update(self, iq, data_center=None):
        n = len(iq) // 2
        if n < 32:
            return
        z = iq[0:2 * n:2].astype(np.float32) + 1j * iq[1:2 * n:2].astype(np.float32)
        w = np.hanning(n).astype(np.float32)
        spec = np.fft.fftshift(np.fft.fft(z * w))
        db = (20 * np.log10(np.abs(spec) / n + 1e-10)).astype(np.float32)
        # Branch G S-meter: peak level WITHIN the receiver passband (offset around
        # the VFO), so out-of-passband junk doesn't move the needle.
        self.peak_dbfs = -140.0
        if self.center_hz and self.vfo_hz:
            lo_f = self.vfo_hz + self.filt[0] - self.center_hz
            hi_f = self.vfo_hz + self.filt[1] - self.center_hz
            i1 = int(lo_f / self.rate * len(db)) + len(db) // 2
            i2 = int(hi_f / self.rate * len(db)) + len(db) // 2
            i1, i2 = max(0, min(i1, len(db) - 1)), max(0, min(i2, len(db)))
            if i2 > i1:
                self.peak_dbfs = float(db[i1:i2].max())

        # map each canvas column to its frequency within the DISPLAY span,
        # then sample the full-rate FFT spectrum at that frequency (handles zoom)
        bin_f = (np.arange(len(db)) - len(db) / 2) / len(db) * self.rate   # Hz/bin
        # Thetis model: display centered on the VFO; spectrum + waterfall share
        # the same frame and mapping. Each block is tagged with the DDC center
        # it was captured at (dc_tag). In CTUN mode the display center stays
        # fixed while the DDC follows the VFO, so blocks are placed at their
        # true absolute position (offset = dc_tag - center); in normal mode the
        # offset is zero (center == DDC) and rows are purely relative.
        dc_tag = data_center if data_center else self.center_hz
        disp_rel = (np.arange(CANVAS_W) / CANVAS_W - 0.5) * self.span
        col = np.interp(disp_rel, bin_f, db).astype(np.float32)
        off = int(round((dc_tag - self.center_hz) / max(1.0, self.span) * CANVAS_W))
        if off:
            col = np.roll(col, off)
            if off > 0:
                col[:off] = col[off]
            else:
                col[off:] = col[off - 1]
        # smooth with a small gaussian kernel (sigma ~1.2 px) to remove stair-steps
        k = np.exp(-0.5 * (np.arange(-2, 3) / 0.9) ** 2)
        k /= k.sum()
        # edge-padded convolve (zero padding would inject 0 dB ghosts at the edges)
        col = np.convolve(np.pad(col, 2, mode="edge"), k, mode="valid").astype(np.float32)
        # 2-frame EMA per bin (video averaging) - kills waterfall speckle while
        # keeping the spectrum trace responsive (Quisk peak_hold analogue)
        prev = getattr(self, "_col_prev", None)
        if prev is not None and len(prev) == len(col):
            col = 0.5 * prev + 0.5 * col
        self._col_prev = col
        self._col = col

        # adaptive contrast: track the noise floor and stretch the display
        # range around it (like Thetis does) so weak signals stay visible
        floor = float(np.percentile(col, 10))
        if getattr(self, "_floor", None) is None:
            self._floor = floor   # first frame: no smoothing (avoid garbage init)
        else:
            self._floor = 0.8 * self._floor + 0.2 * floor
        # waterfall range covers the noise envelope (p95-p10) so speckle stays dark
        p95 = float(np.percentile(col, 95))
        self._wfrange = max(20.0, (p95 - floor) + 8.0)
        # Quisk-style y_zero/y_scale: user slider shifts the zero point (dB)
        # relative to the auto noise floor; y_scale sets the dB span shown
        lo = self._floor - 18.0 + self.y_zero
        hi = self._floor + self.y_scale + self.y_zero
        norm = np.clip((col - lo) / (hi - lo), 0, 1)
        # waterfall gets its own softer normalization: noise sits in the dark
        # blue/purple zone (Quisk waterfall_y_zero/y_scale behavior).
        # wnorm: 0 at noise floor, 1.0 at ~+48 dB above floor. Quisk palette:
        # 0=black, 0.14=blue-purple, 0.29=purple, 0.43=magenta-pink,
        # 0.57=orange, 0.71=light green, 0.86=yellow, 1.0=white
        wnorm = np.clip((col - self._floor) / getattr(self, "_wfrange", 30.0), 0, 1)
        wnorm = np.power(wnorm, getattr(self, "wf_gamma", 1.0))   # intensity slider
        self._norm_lo, self._norm_hi = lo, hi

        self._wf_img = np.roll(self._wf_img, 1, axis=0)   # newest at top
        self._wf_img[0] = self._cmap(wnorm)               # low freq left (same order as spectrum)
        self._draw()

    # Thetis-style high-contrast palette: black -> deep blue -> cyan ->
    # green -> yellow -> red -> white (like the Thetis waterfall)
    _CMAP_CACHE = None

    @classmethod
    def _build_cmap(cls):
        if cls._CMAP_CACHE is not None:
            return cls._CMAP_CACHE
        # Authentic Quisk default waterfallPalette (8 stops)
        stops = [
            (0.00, (0, 0, 0)),
            (36/255, (85, 0, 255)),
            (73/255, (153, 0, 255)),
            (109/255, (255, 0, 128)),
            (146/255, (255, 119, 0)),
            (182/255, (85, 255, 100)),
            (219/255, (255, 255, 0)),
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
        """Compute numpy arrays only (called from any thread). Tk-safe."""
        col = getattr(self, "_col", None)
        if col is None:
            return
        pan = np.zeros((PAN_H, CANVAS_W, 3), dtype=np.uint8)
        # Quisk-style lemonchiffon background
        pan[:] = (255, 250, 205)
        lo = getattr(self, "_norm_lo", self.DB_BOT)
        hi = getattr(self, "_norm_hi", self.DB_TOP)
        ys = (PAN_H - 1 - np.clip(
            (col - lo) / max(1e-6, hi - lo) * (PAN_H - 1),
            0, PAN_H - 1)).astype(np.int32)
        # filter bandwidth rectangle drawn BEHIND the trace (Quisk lemonchiffon3)
        if self.center_hz and self.vfo_hz:
            fx1 = self.f2x(self.vfo_hz + self.filt[0])
            fx2 = self.f2x(self.vfo_hz + self.filt[1])
            x1c, x2c = max(0, int(fx1)), min(CANVAS_W, int(fx2))
            if x2c > x1c:
                pan[:, x1c:x2c] = (205, 201, 165)   # lemonchiffon3
        # horizontal gray grid lines every 10 dB (Quisk color_gl = grey)
        # grid computed from current dB scale
        lo_d, hi_d = lo, hi
        step = 10.0
        first = np.ceil(lo_d / step) * step
        f = first
        while f < hi_d:
            gy = int(PAN_H - 1 - (f - lo_d) / max(1e-6, hi_d - lo_d) * (PAN_H - 1))
            if 0 <= gy < PAN_H:
                pan[gy, :] = (190, 190, 190)
            f += step
        # dark green connected trace (Quisk color_graphline #005500)
        trace = np.array([0, 85, 0], dtype=np.uint8)
        xs_all = np.arange(CANVAS_W)
        for dy in (-1, 0):
            yy = np.clip(ys + dy, 0, PAN_H - 1)
            pan[yy, xs_all] = trace
        for x in range(1, CANVAS_W):
            y0, y1 = int(ys[x - 1]), int(ys[x])
            if abs(y1 - y0) > 1:
                if y0 > y1:
                    y0, y1 = y1, y0
                yy = np.arange(y0 + 1, y1)
                if len(yy):
                    pan[np.clip(yy, 0, PAN_H - 1), x] = trace

        # finished composite (pan + waterfall); UI thread blits it
        self._ready = np.vstack([pan, self._wf_img])
        self._ready_ys = ys

    def _blit(self):
        """Main-thread only: convert finished arrays to PhotoImage + draw overlays."""
        arr = getattr(self, "_ready", None)
        if arr is None:
            return
        self.delete("all")
        if self._pil:
            self._photo = ImageTk.PhotoImage(Image.fromarray(arr))
            self.create_image(0, 0, image=self._photo, anchor="nw")
        # thin dark-green vector trace on top for crispness (Quisk color_graphline)
        ys = getattr(self, "_ready_ys", None)
        if ys is not None:
            pts = []
            for x in range(0, CANVAS_W):
                pts += [x, ys[x]]
            self.create_line(pts, fill="#005500", width=1,
                             smooth=True, splinesteps=8)
        self._draw_overlays()

    def _draw_overlays(self):
        """Quisk-style overlays: black dB labels on cream, shared X axis strip
        between graph and waterfall, red tuning line through both panes."""
        lo = getattr(self, "_norm_lo", self.DB_BOT)
        hi = getattr(self, "_norm_hi", self.DB_TOP)
        # dB labels at the 10 dB grid lines, black text (Quisk color_graphticks)
        step = 10.0
        f = np.ceil(lo / step) * step
        while f < hi:
            gy = int(PAN_H - 1 - (f - lo) / max(1e-6, hi - lo) * (PAN_H - 1))
            if 14 <= gy <= PAN_H - 6:
                self.create_text(4, gy - 7, anchor="nw", text=f"{f:.0f}",
                                 fill="#000000", font=("Segoe UI", 8))
            f += step
        # ---- shared X axis strip between graph and waterfall (Quisk layout) ----
        axis_y = PAN_H
        self.create_rectangle(0, axis_y, CANVAS_W, axis_y + 18,
                              fill=("#%02x%02x%02x" % ((255, 250, 205))),
                              outline="")
        self.create_line(0, axis_y, CANVAS_W, axis_y, fill="#000000")
        self.create_line(0, axis_y + 18, CANVAS_W, axis_y + 18, fill="#000000")
        # ticks: choose a label step so labels are >= 50 px apart (1-2-5 series)
        px_per_hz = CANVAS_W / max(1.0, self.span)
        cand = [100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000]
        lstep = next((c for c in cand if c * px_per_hz >= 60), 100000)
        if self.center_hz:
            f0 = self.center_hz - self.span / 2
            f1 = self.center_hz + self.span / 2
            tick = lstep // 5
            start = int(np.floor(f0 / tick)) * tick
            t = start
            while t <= f1:
                x = self.f2x(t)
                if 0 <= x <= CANVAS_W:
                    major = (t % lstep == 0)
                    mid = (not major and t % (lstep // 2) == 0)
                    ln = 8 if major else (5 if mid else 3)
                    self.create_line(x, axis_y + 18 - ln, x, axis_y + 18,
                                     fill="#000000")
                    if major and 12 <= x <= CANVAS_W - 12:
                        khz = int(round(t / 1000))
                        self.create_text(x, axis_y + 2, text=str(khz),
                                         fill="#000000",
                                         font=("Segoe UI", 8))
                t += tick
            # thick center tick marking the exact center frequency
            cx = self.f2x(self.center_hz)
            if 0 <= cx <= CANVAS_W:
                self.create_line(cx, axis_y, cx, axis_y + 12,
                                 fill="#000000", width=3)
        # ---- tuning line: Quisk color_txline red, full height both panes ----
        if self.center_hz:
            x = self.f2x(self.vfo_hz)
            if 0 <= x <= CANVAS_W:
                self.create_line(x, 0, x, PAN_H + WF_H,
                                 fill="#ff0000", width=1)
                # waterfall section drawn brighter for XOR-like visibility
                self.create_line(x, PAN_H + 18, x, PAN_H + WF_H,
                                 fill="#ff4040", width=1)


# ================================================================ app

class MiniTCI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MiniTCI — simplified Thetis radio")
        self.configure(bg="#cfd4dd")
        self.resizable(False, False)   # fixed-size transceiver panel

        self.client = None
        self.connected = False
        self.ptt = False
        self.freq_hz = 14_074_000
        self.mode = "USB"
        self.volume = 0.25
        self.mic_gain = 0.5
        self.smeter = -140.0
        self.tx_tail_s = 0.8
        self.mic_stream = None
        self.tx_audio_q = collections.deque(maxlen=64)
        self.chrono_reqs = collections.deque()
        self.chrono_lock = threading.Lock()
        self.tx_pos = 0
        self._iq_acc_bytes = bytearray()

        self.audio_blocks = collections.deque()
        self.audio_pos = 0
        self.out_stream = None
        self._iq_q = queue.Queue(maxsize=8)
        self.text_q = queue.Queue()
        self.iq_q = queue.Queue(maxsize=4)

        self._build_ui()
        self._bind_slider_wheel()
        # size the window exactly to the widgets (no dead space)
        self.update_idletasks()
        self.geometry("")
        self._load_settings()
        self._open_output()
        self.after(50, self._poll_safe)
        self._bind_settings_autosave()

    # ---------------- settings persistence ----------------
    SETTINGS_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                                 "MiniTCI", "settings.json")

    def _settings_snapshot(self):
        return {
            "freq": self.freq_hz,
            "mode": self.mode_var.get(),
            "volume": self.vol_var.get(),
            "mic_gain": self.mic_var.get(),
            "agc_mode": self.agc_var.get(),
            "agc_gain": self.agc_gain_var.get(),
            "y_zero": self.yzero_var.get(),
            "y_scale": self.yscale_var.get(),
            "zoom": self.zoom_var.get(),
            "wf_gain": self.wf_gain_var.get(),
            "ctun": self.ctun_var.get(),
            "tx_tail_ms": int(self.tx_tail_s * 1000),
            "out_dev": self.out_dev_var.get(),
            "in_dev": self.in_dev_var.get(),
            "host": getattr(self, "host_var", None).get() if hasattr(self, "host_var") else None,
        }

    def _save_settings(self, *_):
        try:
            os.makedirs(os.path.dirname(self.SETTINGS_PATH), exist_ok=True)
            with open(self.SETTINGS_PATH, "w") as f:
                json.dump(self._settings_snapshot(), f, indent=1)
        except OSError:
            pass

    def _load_settings(self):
        try:
            with open(self.SETTINGS_PATH) as f:
                s = json.load(f)
        except (OSError, ValueError):
            return
        try:
            if s.get("freq"):
                self.freq_hz = int(s["freq"])
                self._fmt_freq()
                self.pan.vfo_hz = self.freq_hz
                self.pan.center_hz = self.freq_hz
            for key, var in (("mode", self.mode_var), ("agc_mode", self.agc_var),
                             ("out_dev", self.out_dev_var), ("in_dev", self.in_dev_var)):
                if s.get(key):
                    var.set(s[key])
            for key, var in (("volume", self.vol_var), ("mic_gain", self.mic_var),
                             ("agc_gain", self.agc_gain_var), ("y_zero", self.yzero_var),
                             ("y_scale", self.yscale_var), ("zoom", self.zoom_var),
                             ("wf_gain", self.wf_gain_var)):
                if s.get(key) is not None:
                    var.set(float(s[key]))
            # agc_auto checkbox removed (AGC state = mode dropdown)
            if s.get("ctun") is not None:
                self.ctun_var.set(bool(s["ctun"]))
            if s.get("tx_tail_ms") is not None:
                self.tx_tail_s = max(0.0, min(5.0, float(s["tx_tail_ms"]) / 1000.0))
                self.txtail_var.set(int(self.tx_tail_s * 1000))
            if s.get("host") and hasattr(self, "host_var"):
                self.host_var.set(s["host"])
        except (KeyError, ValueError, tk.TclError):
            pass

    def _bind_slider_wheel(self):
        """Bind mouse wheel to every ttk.Scale: wheel up = increase, down =
        decrease by 2% of range (hold Shift for fine 0.5%)."""
        def wheel(scale, var, lo, hi):
            def handler(e):
                step = (hi - lo) * (0.005 if (e.state & 0x0001) else 0.02)
                d = step if getattr(e, "delta", 120) > 0 else -step
                var.set(min(hi, max(lo, var.get() + d)))
                return "break"
            scale.bind("<MouseWheel>", handler)
            scale.bind("<Button-4>", handler)
            scale.bind("<Button-5>", handler)
        for scale, var, lo, hi in (
                (self.vol_scale if hasattr(self, "vol_scale") else None, self.vol_var, 0, 100),
                (self.mic_scale if hasattr(self, "mic_scale") else None, self.mic_var, 0, 100),
                (self.agc_gain_scale, self.agc_gain_var, -20, 120),
                (self.yzero_scale if hasattr(self, "yzero_scale") else None, self.yzero_var, -40, 40),
                (self.yscale_scale if hasattr(self, "yscale_scale") else None, self.yscale_var, 20, 90),
                (self.zoom_scale if hasattr(self, "zoom_scale") else None, self.zoom_var, 0, 100),
                (self.wf_scale if hasattr(self, "wf_scale") else None, self.wf_gain_var, 0, 100)):
            if scale is not None:
                wheel(scale, var, lo, hi)

    def _bind_settings_autosave(self):
        # save on every user-visible change (traces already registered for vars;
        # add a global save on mouse release / focus out via periodic snapshot)
        def poll_save():
            try:
                snap = self._settings_snapshot()
            except Exception:
                snap = None
            if snap != getattr(self, "_last_snap", None):
                self._last_snap = snap
                self._save_settings()
            self.after(1500, poll_save)
        self.after(1500, poll_save)

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
                                    values=["OFF", "FAST", "MED", "SLOW", "LONG"])
        self.agc_box.pack(side="left")
        self.agc_var.trace_add("write", self._agc_changed)
        # Gain slider: only meaningful when AGC = OFF (fixed-gain / manual mode)
        ttk.Label(r1, text="Gain (manual):", padding=(8, 0, 2, 0)).pack(side="left")
        self.agc_gain_var = tk.DoubleVar(value=40)
        self.agc_gain_scale = ttk.Scale(r1, from_=-20, to=120, variable=self.agc_gain_var,
                  length=90, command=self._agc_gain_changed)
        self.agc_gain_scale.pack(side="left", padx=2)
        self.agc_gain_scale.state(["disabled"])

        # --- row 2: frequency
        r2 = ttk.Frame(self); r2.pack(fill="x", padx=10, pady=2)
        self.freq_lbl = tk.Label(r2, text="14.074.000", bg=C["panel"], fg=C["tune"],
                                 font=("Consolas", 24, "bold"))
        self.freq_lbl.pack(side="left", padx=(2, 14))
        self.freq_lbl.bind("<MouseWheel>", self._freq_wheel)
        for txt, hz in (("−10k", -10000), ("−1k", -1000), ("−100", -100),
                        ("+100", 100), ("+1k", 1000), ("+10k", 10000)):
            ttk.Button(r2, text=txt, width=5,
                       command=lambda d=hz: self.tune_to(self.freq_hz + d)
                       ).pack(side="left", padx=2)
        self.ctun_var = tk.BooleanVar(value=False)
        self.ctun_btn = ttk.Checkbutton(r2, text="CTUN", variable=self.ctun_var)
        self.ctun_btn.pack(side="left", padx=(12, 4))
        ttk.Label(r2, text="Direct kHz:", padding=(12, 0, 2, 0)).pack(side="left")
        self.tune_entry = ttk.Entry(r2, width=10)
        self.tune_entry.pack(side="left")
        self.tune_entry.bind("<Return>", lambda e: self._tune_direct())
        ttk.Button(r2, text="Go", width=4, command=self._tune_direct).pack(side="left", padx=4)

        # --- panadapter + waterfall
        self.pan = PanFall(self)
        self.pan.pack(fill="both", expand=True, padx=10, pady=4)
        self.pan.bind("<Button-1>", self._pan_click)
        self.pan.bind("<B1-Motion>", self._pan_drag)
        self.pan.bind("<ButtonRelease-1>", self._pan_release)
        self.pan.bind("<Button-3>", self._pan_right)
        self.pan.bind("<MouseWheel>", self._pan_wheel)
        self.pan.bind("<Button-4>", self._pan_wheel)   # linux wheel up
        self.pan.bind("<Button-5>", self._pan_wheel)   # linux wheel down
        self.pan.bind("<Motion>", self._pan_motion)

        # --- Quisk-style display adjust row: Y zero + Y scale + span zoom
        rz = ttk.Frame(self); rz.pack(fill="x", padx=10, pady=(0, 2))
        ttk.Label(rz, text="Y zero:").pack(side="left")
        self.yzero_var = tk.DoubleVar(value=0)
        self.yzero_scale = ttk.Scale(rz, from_=-40, to=40, variable=self.yzero_var, length=130,
                  command=self._yzero_changed)
        self.yzero_scale.pack(side="left", padx=4)
        ttk.Label(rz, text="Y scale:").pack(side="left", padx=(14, 0))
        self.yscale_var = tk.DoubleVar(value=42)
        self.yscale_scale = ttk.Scale(rz, from_=20, to=90, variable=self.yscale_var, length=130,
                  command=self._yscale_changed)
        self.yscale_scale.pack(side="left", padx=4)
        ttk.Label(rz, text="Zoom:").pack(side="left", padx=(14, 0))
        self.zoom_var = tk.DoubleVar(value=0)
        self.zoom_scale = ttk.Scale(rz, from_=0, to=100, variable=self.zoom_var, length=130,
                  command=self._zoom_changed)
        self.zoom_scale.pack(side="left", padx=4)
        self.zoom_lbl = ttk.Label(rz, text="96 kHz")
        self.zoom_lbl.pack(side="left", padx=6)
        ttk.Label(rz, text="WF intensity:").pack(side="left", padx=(14, 0))
        self.wf_gain_var = tk.DoubleVar(value=50)
        self.wf_scale = ttk.Scale(rz, from_=0, to=100, variable=self.wf_gain_var, length=130,
                  command=self._wf_gain_changed)
        self.wf_scale.pack(side="left", padx=4)

        # --- row 3: volume + sound devices + smeter
        r3 = ttk.Frame(self); r3.pack(fill="x", padx=10, pady=2)
        ttk.Label(r3, text="Volume:").pack(side="left")
        self.vol_var = tk.DoubleVar(value=70)
        self.vol_scale = ttk.Scale(r3, from_=0, to=100, variable=self.vol_var, length=140,
                  command=self._vol_changed)
        self.vol_scale.pack(side="left", padx=4)
        self.volume = 0.7 * 1.2

        self._out_devs = list_output_devices()
        self._in_devs = list_input_devices()
        ttk.Label(r3, text="Speaker:", padding=(10, 0, 2, 0)).pack(side="left")
        self.out_dev_var = tk.StringVar(value="(system default)")
        out_names = ["(system default)"] + [n for _, n, _ in self._out_devs]
        ttk.Combobox(r3, textvariable=self.out_dev_var, width=22, state="readonly",
                     values=out_names).pack(side="left", padx=2)
        self.out_dev_var.trace_add("write", lambda *_: self._reopen_output())

        # analog-style S-meter: S0..S9 scale + dB over S9, peak-hold needle
        self.sm = tk.Canvas(r3, width=310, height=68, bg=C["panel"], highlightthickness=0)
        self.sm.pack(side="left", padx=12)
        smL = 8; smR = 302; smY = 26
        self._sm_x0, self._sm_x1, self._sm_y = smL, smR, smY
        # colored zone bar: S0-S9 green, +0..+20 amber, >+20 red
        self.sm.create_rectangle(smL, smY - 11, smR, smY, fill="#dddddd", outline="#999999")
        # scale mapping: -127..-15 dBFS across the bar; S9 at -35
        def smx(db): return smL + (db + 127.0) / 112.0 * (smR - smL)
        x_s9 = smx(-35)
        self.sm.create_rectangle(smL, smY - 11, x_s9, smY, fill="#3fa34d", outline="")
        self.sm.create_rectangle(x_s9, smY - 11, smx(-25), smY, fill="#e0a63a", outline="")
        self.sm.create_rectangle(smx(-25), smY - 11, smR, smY, fill="#c0392b", outline="")
        # ticks + labels S1..S9, +10, +20
        for n in range(1, 10):
            db = -124.0 + n * 10.0   # S1=-114 ... S9=-34 approx per IARU-ish
            x = smx(db)
            self.sm.create_line(x, smY - 11, x, smY - 16, fill="#444444")
            self.sm.create_text(x, smY - 23, text=str(n), fill="#333333",
                                font=("Segoe UI", 8, "bold"))
        for db, lab in ((-25, "+10"), (-17, "+20")):
            x = smx(db)
            self.sm.create_line(x, smY - 11, x, smY - 16, fill="#444444")
            self.sm.create_text(x, smY - 23, text=lab, fill="#8a4a10",
                                font=("Segoe UI", 8, "bold"))
        self.sm.create_text(smL - 2, smY - 18, text="S", fill="#444444",
                            font=("Segoe UI", 7, "bold"))
        # needle (current) + peak-hold tick
        self.sm_bar = self.sm.create_line(smL, smY + 3, smL, smY + 12,
                                          fill="#1a1f29", width=3)
        self.sm_peak = self.sm.create_line(smL, smY - 11, smL, smY - 4,
                                           fill="#c0392b", width=3)
        self.sm_txt = self.sm.create_text(smR, 60, text="−140 dBFS", anchor="e",
                                          fill=C["fg"], font=("Consolas", 11, "bold"))
        self._sm_peak_db = -140.0
        self.state_lbl = tk.Label(r3, text="● disconnected", bg=C["panel"], fg=C["dim"],
                                  font=("Segoe UI", 9))
        self.state_lbl.pack(side="right")

        # --- row 4: TX
        r4 = ttk.Frame(self); r4.pack(fill="x", padx=10, pady=4)
        self.ptt_btn = tk.Button(r4, text="PTT", bg="#f4d7d4", fg=C["fg"], width=8,
                                 font=("Segoe UI", 10, "bold"))
        self.ptt_btn.bind("<ButtonPress-1>", lambda e: self.ptt_on())
        self.ptt_btn.bind("<ButtonRelease-1>", lambda e: self.ptt_off())
        ttk.Label(r4, text="TX tail (ms):", padding=(14, 0, 2, 0)).pack(side="left")
        self.txtail_var = tk.IntVar(value=200)
        self.txtail_entry = ttk.Entry(r4, width=5)
        self.txtail_entry.insert(0, "200")
        self.txtail_entry.pack(side="left")
        self.txtail_entry.bind("<Return>", self._txtail_entry)
        self.txtail_entry.bind("<FocusOut>", self._txtail_entry)
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
        self.mic_scale = ttk.Scale(r4, from_=0, to=100, variable=self.mic_var, length=120,
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
                          blocksize=1024)
            if dev is not None:
                kwargs["device"] = dev
            self.out_stream = sd.OutputStream(**kwargs)
            self.out_stream.start()
            if not getattr(self, "_pump_started", False):
                self._pump_started = True
                threading.Thread(target=self._audio_pump, daemon=True).start()
            name = "default" if dev is None else self.out_dev_var.get()
            self.logprint(f"output device: {name}")
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

    def _audio_pump(self):
        """Blocking audio writer on its own thread - immune to Tk/GIL stalls.
        sounddevice blocking write() has its own internal timing; this thread
        only needs to feed chunks slightly faster than real time."""
        CHUNK = 1024
        mono_buf = np.zeros(CHUNK, dtype=np.float32)
        while True:
            try:
                if self.out_stream is None:
                    time.sleep(0.1)
                    continue
                now_t = time.time()
                if self.ptt or now_t < getattr(self, "_tx_mute_until", 0.0):
                    # TX: mute the RX monitor - no self-hearing in the speakers.
                    # Keep muting briefly AFTER PTT release: the RF chain (IC-7100
                    # unkeying + AGC decay) still carries the tail of the
                    # transmission for ~1 s, which would play as an annoying
                    # echo of your own voice.
                    self.audio_blocks.clear()
                    self.audio_pos = 0
                    stereo = np.zeros((CHUNK, 2), dtype=np.float32)
                    try:
                        self.out_stream.write(stereo)
                    except Exception:
                        pass
                    continue
                filled = 0
                while filled < CHUNK:
                    if not self.audio_blocks:
                        mono_buf[filled:] = 0.0
                        break
                    blk = self.audio_blocks[0]
                    take = min(len(blk) - self.audio_pos, CHUNK - filled)
                    if take <= 0:
                        self.audio_blocks.popleft()
                        self.audio_pos = 0
                        continue
                    mono_buf[filled:filled + take] = blk[self.audio_pos:self.audio_pos + take]
                    filled += take
                    self.audio_pos += take
                    if self.audio_pos >= len(blk):
                        self.audio_blocks.popleft()
                        self.audio_pos = 0
                stereo = np.empty((CHUNK, 2), dtype=np.float32)
                stereo[:, 0] = mono_buf * self.volume
                stereo[:, 1] = mono_buf * self.volume
                try:
                    self.out_stream.write(stereo)
                except Exception:
                    time.sleep(0.05)
            except Exception:
                time.sleep(0.1)

    def _vol_changed(self, v):
        # The server-side slice AF gain is now Thetis-like (0.5), so the slider
        # is a plain output attenuator: 0..100 -> 0..1.2 (+1.6 dB max headroom)
        self.volume = (float(v) / 100.0) * 1.2

    def _txtail_entry(self, *_):
        try:
            ms = int(float(self.txtail_var.get()))
        except (ValueError, tk.TclError):
            self.txtail_var.set(int(self.tx_tail_s * 1000))
            return
        ms = max(0, min(5000, ms))
        self.tx_tail_s = ms / 1000.0
        self.txtail_var.set(ms)

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
        # Server streams ~750 tiny IQ frames/s; per-frame np.concatenate copies
        # starve the asyncio loop (GIL) -> ping timeouts -> disconnects. Accumulate
        # raw bytes instead (O(1) append) and convert once per block.
        try:
            self._iq_acc_bytes.extend(data.tobytes())
            target = 16384 * 4  # 16384 interleaved values = 8192 complex = 11.7 Hz/bin @ 96k
            while len(self._iq_acc_bytes) >= target:
                block = np.frombuffer(bytes(self._iq_acc_bytes[:target]), dtype="<f4")
                del self._iq_acc_bytes[:target]
                try:
                    # tag each block with the DDC center in effect when it arrived,
                    # so queued stale blocks are labeled correctly (sync fix)
                    self._iq_q.put_nowait((block, rate, float(self.freq_hz)))
                except Exception:
                    # UI stalled - drop the OLDEST block so new data keeps flowing
                    try:
                        self._iq_q.get_nowait()
                        self._iq_q.put_nowait((block, rate))
                    except Exception:
                        pass
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
        self._iq_acc_bytes = bytearray()
        self._last_draw = 0.0
        self._pan_job_q = queue.Queue(maxsize=4)
        self._pan_thread_started = False
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
            if self.agc_var.get() == "OFF":
                self.send("agc_auto_ex:0,false;")
                self.send(f"agc_gain:0,{int(self.agc_gain_var.get())};")
            else:
                self.send(f"agc_mode:0,{self._agc_mode_to_tci(self.agc_var.get())};")
                self.send("agc_auto_ex:0,true;")
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
    def _poll_inner(self):
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
            data, rate, dcenter = self._iq_q.get_nowait()
            if rate and int(rate) != int(self.pan.rate):
                self.pan.rate = float(rate)
                self.pan.span = min(max(self.pan.span, 24000), float(rate))
            now2 = time.time()
            if now2 - getattr(self, "_last_draw", 0) > 0.08:   # ~12 fps max
                self._last_draw = now2
                # drain to the newest block (skip stale ones), then compute + blit
                # synchronously: pan.update is pure numpy (~5ms at 8192 samples),
                # and _blit needs the result immediately - a worker thread here
                # races the blit and paints nothing on first connect.
                data2, rate2, dc2 = data, rate, dcenter
                while True:
                    try:
                        data2, rate2, dc2 = self._iq_q.get_nowait()
                    except queue.Empty:
                        break
                if int(rate2) != int(self.pan.rate):
                    self.pan.rate = float(rate2)
                self.pan.update(data2, dc2)
                self.pan._blit()
        except (queue.Empty, AttributeError):
            pass

        self.service_tx_audio()
        self._draw_smeter()
        self.after(50, self._poll_safe)

    def _poll_safe(self):
        """Exception-proof: a bug in _poll must never kill the UI loop."""
        try:
            self._poll_inner()
        except Exception:
            import traceback
            self.logprint("poll error: " + traceback.format_exc(limit=2))
            self.after(200, self._poll_safe)

    def _pan_thread(self):
        """Single long-lived worker: FFT + waterfall roll, no thread churn."""
        while True:
            try:
                data = self._pan_job_q.get()
                if data is None:
                    continue
                self.pan.update(data)
            except Exception:
                pass

    def _start_pan_thread(self):
        if not getattr(self, "_pan_thread_started", False):
            self._pan_thread_started = True
            self._pan_job_q = queue.Queue(maxsize=4)
            threading.Thread(target=self._pan_thread, daemon=True).start()

    def _handle(self, d):
        for k, v in d.items():
            if k == "vfo" and v:
                p = v.split(",")
                if len(p) >= 3:
                    try:
                        hz = float(p[2])
                        self.freq_hz = int(hz)
                        self._fmt_freq()
                        self.pan.vfo_hz = hz
                        self.pan.data_center_hz = hz
                        if not self.pan.center_hz:
                            self.pan.center_hz = hz
                    except ValueError:
                        pass
            elif k == "dds" and v:
                try:
                    dds_hz = float(v.split(",")[-1])
                    self.pan.data_center_hz = dds_hz   # actual DDC center of the IQ data
                    if not self.pan.center_hz:
                        self.pan.center_hz = dds_hz
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
                    self.pan.rate = float(int(v))
                except ValueError:
                    pass

    def _fmt_freq(self):
        # Thetis-style: MHz.kHz.Hz with dots, e.g. 14.074.000
        mhz = self.freq_hz // 1_000_000
        khz = (self.freq_hz % 1_000_000) // 1000
        hz = self.freq_hz % 1000
        self.freq_lbl.config(text=f"{mhz}.{khz:03d}.{hz:03d}")

    def _freq_wheel(self, e):
        # wheel on the frequency display: 100 Hz steps (shift = 10 Hz fine)
        step = 10 if e.state & 0x0001 else 100
        d = step if getattr(e, "delta", 120) > 0 else -step
        self.tune_to(self.freq_hz + d)

    def _draw_smeter(self):
        # IQ-derived peak-bin dBFS displayed on an analog S-scale.
        # dBFS -> S-unit: S9 = -35 dBFS, each S-unit 10 dB below (S1 = -115).
        if self.ptt:
            # TX: the meter shows the MIC/voice level driving the transmitter
            db = getattr(self, "mic_level_db", -140.0)
            self.smeter_db = db
        else:
            db = getattr(self.pan, "peak_dbfs", None)
            if db is None:
                db = self.smeter
            self.smeter_db = db
        x0, x1 = self._sm_x0, self._sm_x1
        frac = clamp((db + 127.0) / 112.0, 0.0, 1.0)
        x = x0 + frac * (x1 - x0)
        self.sm.coords(self.sm_bar, x, self._sm_y + 3, x, self._sm_y + 12)
        # peak hold: rises instantly, decays slowly
        pk = max(db, self._sm_peak_db - 0.4)
        self._sm_peak_db = clamp(pk, -140.0, 0.0)
        pf = clamp((self._sm_peak_db + 127.0) / 112.0, 0.0, 1.0)
        px = x0 + pf * (x1 - x0)
        self.sm.coords(self.sm_peak, px, self._sm_y - 11, px, self._sm_y - 4)
        # S-unit readout
        s_units = max(0.0, min(9.0, (db + 115.0) / 10.0))
        over = db - (-35.0)
        if db >= -35.0:
            txt = f"S9+{over:.0f} dB  ({db:.0f} dBFS)"
        else:
            txt = f"S{s_units:.1f}  ({db:.0f} dBFS)"
        self.sm.itemconfig(self.sm_txt, text=txt)

    # ---------------- controls ----------------
    def send(self, cmd):
        if self.client and self.client.loop:
            self.client.send(cmd)
            if "audio_start" in cmd:
                self.client._streaming = True
            elif "audio_stop" in cmd:
                self.client._streaming = False

    def nudge(self, hz):
        self.tune_to(self.freq_hz + hz)

    def tune_to(self, hz):
        self.freq_hz = int(hz)
        self._fmt_freq()
        if self.ctun_var.get():
            # CTUN: display (and waterfall history) stays fixed; only the tune
            # line moves. If the VFO would leave the window, slide the display
            # minimally to keep it visible (Thetis does the same).
            self.pan.vfo_hz = self.freq_hz
            half = self.pan.span / 2 * 0.95
            if abs(self.freq_hz - self.pan.center_hz) > half:
                df = self.freq_hz - self.pan.center_hz
                self.pan.shift_waterfall(df)
                self.pan.center_hz = self.freq_hz
        else:
            # Normal: tuning slides the WHOLE panafall (past rows included).
            df = self.freq_hz - self.pan.center_hz
            if self.pan.center_hz and df:
                self.pan.shift_waterfall(df)
            self.pan.center_hz = self.freq_hz
            self.pan.vfo_hz = self.freq_hz
        self.pan.data_center_hz = self.freq_hz
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
        mode = self.agc_var.get()
        manual = (mode == "OFF")
        # enable the Gain slider only in manual mode
        try:
            self.agc_gain_scale.state(["!disabled"] if manual else ["disabled"])
        except tk.TclError:
            pass
        if not self.connected:
            return
        if manual:
            # fixed-gain mode: gain slider controls the receiver gain directly
            self.send("agc_auto_ex:0,false;")
            self.send(f"agc_gain:0,{int(self.agc_gain_var.get())};")
        else:
            # AGC on: FAST/MED/SLOW/LONG = attack/decay speed presets
            self.send(f"agc_mode:0,{self._agc_mode_to_tci(mode)};")
            self.send("agc_auto_ex:0,true;")

    def _agc_auto_changed(self):
        # kept for compatibility (Auto checkbox removed); no-op
        pass

    def _agc_gain_changed(self, v):
        if self.connected and self.agc_var.get() == "OFF":
            self.send(f"agc_gain:0,{int(float(v))};")

    def _hit_test(self, x):
        """Classify click position: 'in-filter', 'edge-lo', 'edge-hi', or 'span'."""
        if not self.pan.center_hz:
            return "span"
        fx1 = self.pan.f2x(self.pan.vfo_hz + self.pan.filt[0])
        fx2 = self.pan.f2x(self.pan.vfo_hz + self.pan.filt[1])
        tol = 3
        # avoid edge grabbing when the passband is narrow on screen (< 8px):
        # then the whole band acts as a grab handle
        if fx2 - fx1 < 8:
            return "in-filter" if fx1 - tol <= x <= fx2 + tol else "span"
        if abs(x - fx1) <= tol:
            return "edge-lo"
        if abs(x - fx2) <= tol:
            return "edge-hi"
        if fx1 < x < fx2:
            return "in-filter"
        return "span"

    def _pan_click(self, e):
        # Quisk OnLeftDown: record start; choose tx/rx target (we only have one VFO)
        self._drag_x = e.x
        self._drag_y = e.y
        self._drag_center = self.pan.center_hz
        self._drag_vfo = self.pan.vfo_hz
        self._drag_filt = self.pan.filt
        self._moved = False

    def _pan_drag(self, e):
        # Quisk OnMotion: dragging tunes the frequency; drag speed scales with
        # height above the X axis (near the axis = fine, top = coarse)
        if not self.pan.center_hz or not hasattr(self, "_drag_x"):
            return
        if abs(e.x - self._drag_x) > 2 or abs(e.y - getattr(self, "_drag_y", e.y)) > 2:
            self._moved = True
        if not self._moved:
            return
        # Quisk: speed = max(10, originY - mouse_y) / (originY + 1)
        speed = max(10.0, PAN_H - e.y) / float(PAN_H + 1)
        dx_hz = speed * (e.x - self._drag_x) / CANVAS_W * self.pan.span
        self._drag_x = e.x   # Quisk accumulates per-motion deltas
        self.pan.vfo_hz = self._drag_vfo = self._drag_vfo + dx_hz
        self._freq_pending = self.pan.vfo_hz
        # live-follow the frequency display (command still sent on release)
        self.freq_hz = int(self.pan.vfo_hz)
        self._fmt_freq()

    def _pan_release(self, e):
        moved = getattr(self, "_moved", False)
        if self.pan.center_hz and not moved:
            f = self.pan.x2f(e.x)
            if self.mode in ("CWU", "CWL"):
                f = self._cw_snap(f)
            # Quisk OnLeftUp: re-round to the frequency grid
            self.tune_to(self._round_tune(f))
            return
        if moved and getattr(self, "_freq_pending", None):
            self.tune_to(self._round_tune(self._freq_pending))

    def _round_tune(self, f):
        # Quisk OnLeftUp FreqRound: snap tune offset to the wheel step grid
        wm = 50
        return int(round(f / wm)) * wm

    def _cw_snap(self, f_click):
        # Quisk CW peak snap: search +/- filter width for a peak significantly
        # above the local average, then quadratic-interpolate the peak position
        col = getattr(self.pan, "_col", None)
        if col is None or not self.pan.center_hz:
            return f_click
        x = int(self.pan.f2x(f_click))
        cw_hz = max(200.0, (self.pan.filt[1] - self.pan.filt[0]))
        half = max(2, int(cw_h / self.pan.span * CANVAS_W / 2)) if (cw_h := (self.pan.filt[1] - self.pan.filt[0])) else 4
        x1, x2 = max(0, x - half), min(CANVAS_W, x + half)
        if x2 - x1 < 5:
            return f_click
        seg = col[x1:x2]
        xmax = int(np.argmax(seg)) + x1
        avg = float(np.mean(seg))
        if xmax <= x1 or xmax >= x2 - 1 or col[xmax] - avg < 5:
            return f_click
        yp, y0, ym = col[xmax + 1], col[xmax], col[xmax - 1]
        denom = (ym - 2 * y0 + yp)
        corr = 0.5 * (ym - yp) / denom if denom != 0 else 0.0
        corr = max(-0.5, min(0.5, corr))
        return self.pan.x2f(xmax + corr)

    def _pan_right(self, e):
        # Quisk OnRightDown: move the VFO to the clicked frequency, snapped
        # (10 kHz if span > 40k, 1 kHz if > 5k, else 100 Hz)
        if not self.pan.center_hz:
            return
        f = self.pan.x2f(e.x)
        if self.pan.span > 40000:
            step = 10000
        elif self.pan.span > 5000:
            step = 1000
        else:
            step = 100
        vfo = int(round(f / step)) * step
        self.tune_to(vfo)

    def _yzero_changed(self, v):
        try:
            self.pan.y_zero = float(v)
        except (ValueError, tk.TclError):
            pass

    def _yscale_changed(self, v):
        try:
            self.pan.y_scale = float(v)
        except (ValueError, tk.TclError):
            pass

    def _wf_gain_changed(self, v):
        # waterfall intensity/contrast: 0..100 -> gamma 1.6..0.4 applied to the
        # normalized level before the palette (Thetis contrast control analogue)
        try:
            g = float(v)
        except (ValueError, tk.TclError):
            return
        self.pan.wf_gamma = 1.6 - 1.2 * (g / 100.0)

    def _zoom_changed(self, v):
        # Quisk zoom model: effective span = rate * zoom, centered on the VFO
        try:
            z = float(v)
        except (ValueError, tk.TclError):
            return
        if z < 1:
            new_span = 96000.0
        else:
            # 0..100 slider -> 96k..24k logarithmic-ish
            new_span = 96000.0 * (1.0 - 0.75 * z / 100.0)
        new_span = clamp(new_span, 24000, min(384000, self.pan.rate or 96000))
        self.pan.center_hz = self.freq_hz if self.freq_hz else self.pan.center_hz
        self.pan.span = new_span
        self.pan._wf_img[:] = 0   # clear stale rows drawn at the old span
        self.zoom_lbl.config(text=f"{new_span / 1000:.0f} kHz")

    def _pan_motion(self, e):
        if not self.pan.center_hz:
            return
        # Quisk has a plain crosshair over the graph; keep edge hints only
        self.pan.config(cursor="crosshair")

    def _pan_wheel(self, e):
        # Quisk OnWheel: tune in mouse_wheelmod (50 Hz) steps
        if not self.pan.center_hz:
            return
        delta = getattr(e, "delta", 120)
        step = 50 if delta > 0 else -50
        self.tune_to(self.freq_hz + step)

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
        # keep the monitor muted for the TX tail (IC-7100 unkeying + AGC decay)
        self._tx_mute_until = time.time() + self.tx_tail_s
        self.mic_level_db = -140.0
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
            # voice level for the S-meter (dBFS, same scale as RX)
            rms = float(np.sqrt(np.mean(indata.astype(np.float64) ** 2)))
            self.mic_level_db = 20.0 * np.log10(rms + 1e-10)

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