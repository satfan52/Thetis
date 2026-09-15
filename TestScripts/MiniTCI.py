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
TX_AUDIO_RATE = 48000   # TX audio stream rate (negotiated with the server)

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
# Branch H1: total filter-width presets (Hz, applied symmetric around the VFO)
BW_PRESETS = {"5k": 5000, "3.8k": 3800, "2.9k": 2900, "2.7k": 2700, "2.4k": 2400,
              "1.8k": 1800, "1k": 1000, "500": 500, "250": 250}

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
        # Branch H1: sub VFO B cursor (blue) + its filter passband
        self.sub_hz = 0.0       # absolute; 0 = hidden
        self.sub_filt = (150, 2800)
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
        self._last_db = db          # Branch H1/B7: kept for double-click peak search
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
            # Branch H1 fix: columns outside the delivered DDC band have no live
            # data in CTUN (display span == DDC rate). Keep the LAST KNOWN value
            # per column (frozen spectrum) instead of smearing the edge column
            # across the uncovered region - that smear looked like a broken
            # waterfall half.
            prev_col = getattr(self, "_col_prev_raw", None)
            if prev_col is not None and len(prev_col) == len(col):
                if off > 0:
                    col[:off] = prev_col[:off]
                else:
                    col[off:] = prev_col[off:]
        self._col_prev_raw = col.copy()
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

        # Branch H1: passband tints alpha-blended AFTER the trace so the signal
        # stays visible through the color (translucent).
        def _tint(x1c, x2c, rgb, alpha):
            if x2c > x1c:
                region = pan[:, x1c:x2c].astype(np.float32)
                col = np.array(rgb, dtype=np.float32)
                pan[:, x1c:x2c] = (region * (1.0 - alpha) + col * alpha).astype(np.uint8)
        if self.center_hz and self.sub_hz:
            sx1 = self.f2x(self.sub_hz + self.sub_filt[0])
            sx2 = self.f2x(self.sub_hz + self.sub_filt[1])
            _tint(max(0, int(sx1)), min(CANVAS_W, int(sx2)), (172, 206, 240), 0.35)
        if self.center_hz and self.vfo_hz:
            fx1 = self.f2x(self.vfo_hz + self.filt[0])
            fx2 = self.f2x(self.vfo_hz + self.filt[1])
            _tint(max(0, int(fx1)), min(CANVAS_W, int(fx2)), (205, 201, 165), 0.35)

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
        # ---- Branch H1: Thetis-style passband edges (red left / yellow right) ----
        if self.center_hz and self.vfo_hz:
            ex1 = self.f2x(self.vfo_hz + self.filt[0])
            ex2 = self.f2x(self.vfo_hz + self.filt[1])
            if 0 <= ex1 <= CANVAS_W:
                self.create_line(ex1, 0, ex1, PAN_H, fill="#ff3030", width=2)
            if 0 <= ex2 <= CANVAS_W:
                self.create_line(ex2, 0, ex2, PAN_H, fill="#ffd050", width=2)
        if self.center_hz and self.sub_hz:
            ex1 = self.f2x(self.sub_hz + self.sub_filt[0])
            ex2 = self.f2x(self.sub_hz + self.sub_filt[1])
            if 0 <= ex1 <= CANVAS_W:
                self.create_line(ex1, 0, ex1, PAN_H, fill="#3366ff", width=2)
            if 0 <= ex2 <= CANVAS_W:
                self.create_line(ex2, 0, ex2, PAN_H, fill="#77bbff", width=2)
        # ---- tuning line: Quisk color_txline red, full height both panes ----
        if self.center_hz:
            # Branch H1: sub VFO B tuning line (blue) drawn UNDER the red line
            if self.sub_hz:
                xs = self.f2x(self.sub_hz)
                if 0 <= xs <= CANVAS_W:
                    self.create_line(xs, 0, xs, PAN_H + WF_H,
                                     fill="#0066ff", width=1)
                    self.create_line(xs, PAN_H + 18, xs, PAN_H + WF_H,
                                     fill="#4488ff", width=1)
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
        # Branch H1: VFO B / subrx state
        self.sub_hz = 0                    # set at connect: A + 2 kHz (band-correct)
        self.sub_mode = "USB"
        self.sub_filt = (150, 2800)
        self.sub_enabled = False
        self.split = False
        self.tx_vfo = "A"                # which VFO the TX checkbox shows
        self.audio_sel = "main"          # main | sub | both
        self.ddc_center_hz = self.freq_hz  # hardware centre frequency (DDS)
        self.volume = 0.25
        self.mic_gain = 0.5
        self.smeter = -140.0
        self.tx_tail_s = 0.35
        self.tuning = False
        self.tune_phase = 0.0
        self.tune_sample_pos = 0
        self.tune_amp = 0.075
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
                self.tx_tail_s = max(0.0, min(10.0, float(s["tx_tail_ms"]) / 1000.0))
                self.txtail_var.set(int(self.tx_tail_s * 1000))
            if s.get("host") and hasattr(self, "host_var"):
                self.host_var.set(s["host"])
            # Branch H1 restore
            if s.get("sub_hz"):
                self.sub_hz = int(s["sub_hz"])
            if s.get("sub_mode"):
                self.sub_mode = s["sub_mode"]
                self.submode_var.set(self.sub_mode)
            if s.get("sub_filt"):
                fl, fh = int(s["sub_filt"][0]), int(s["sub_filt"][1])
                if fh - fl >= 200 and fh > 0:      # sanity: ignore stale half-width values
                    self.sub_filt = (fl, fh)
            if s.get("balance") is not None:
                self.bal_var.set(float(s["balance"]))
            self._band_stacks = s.get("band_stacks") or {}
            self._scenes = s.get("scenes") or {}
            self._scenes_loaded = True
            for idx in range(4):
                if str(idx) in self._scenes:
                    self.scene_btns[idx].config(text=self._scenes[str(idx)].get("name", f"Scene {idx+1}"))
            self._sub_refresh_ui()
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
        ttk.Label(r1, text="Filter:", padding=(8, 0, 2, 0)).pack(side="left")
        self.filtwidth_var = tk.StringVar(value="2.9k")
        ttk.Combobox(r1, textvariable=self.filtwidth_var, width=5, state="readonly",
                     values=["5k", "3.8k", "2.9k", "2.7k", "2.4k", "1.8k", "1k", "500", "250"]).pack(side="left")
        self.filtwidth_var.trace_add("write", self._filtwidth_changed)

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
        ttk.Label(r2, text="VFO A").pack(side="left", padx=(2, 4))
        self.freq_lbl = tk.Label(r2, text="A 14.074.000", bg=C["panel"], fg=C["tune"],
                                 font=("Consolas", 24, "bold"))
        self.freq_lbl.pack(side="left", padx=(2, 14))
        self.freq_lbl.bind("<MouseWheel>", self._freq_wheel)
        for txt, hz in (("−10k", -10000), ("−1k", -1000), ("−100", -100),
                        ("+100", 100), ("+1k", 1000), ("+10k", 10000)):
            ttk.Button(r2, text=txt, width=5,
                       command=lambda d=hz: self.tune_to(self.freq_hz + d)
                       ).pack(side="left", padx=2)
        self.ctun_var = tk.BooleanVar(value=False)
        self.ctun_btn = ttk.Checkbutton(r2, text="CTUN", variable=self.ctun_var,
                                        command=self._ctun_toggled)
        self.ctun_btn.pack(side="left", padx=(12, 4))
        # Branch H1: hardware centre frequency (middle of the DDS passband)
        self.dds_lbl = tk.Label(r2, text="DDS 7.100.000", bg=C["panel"], fg="#7a4a9a",
                                font=("Consolas", 12, "bold"))
        self.dds_lbl.pack(side="left", padx=(10, 4))
        ttk.Label(r2, text="Direct kHz:", padding=(12, 0, 2, 0)).pack(side="left")
        self.tune_entry = ttk.Entry(r2, width=10)
        self.tune_entry.pack(side="left")
        self.tune_entry.bind("<Return>", lambda e: self._tune_direct())
        ttk.Button(r2, text="Go", width=4, command=self._tune_direct).pack(side="left", padx=4)

        # --- Branch H1 row 2b: VFO B / subrx / split
        r2b = ttk.Frame(self); r2b.pack(fill="x", padx=10, pady=2)
        self.vfo_lbl = tk.Label(r2b, text="VFO B  7.074.000", bg=C["panel"], fg="#0055aa",
                                font=("Consolas", 15, "bold"))
        self.vfo_lbl.pack(side="left", padx=(2, 8))
        self.vfo_lbl.bind("<MouseWheel>", self._sub_wheel)
        ttk.Label(r2b, text="Direct kHz:").pack(side="left", padx=(6, 2))
        self.sub_tune_entry = ttk.Entry(r2b, width=10)
        self.sub_tune_entry.pack(side="left")
        self.sub_tune_entry.bind("<Return>", lambda e: self._sub_tune_direct())
        ttk.Button(r2b, text="Go", width=4, command=self._sub_tune_direct).pack(side="left", padx=4)
        ttk.Label(r2b, text="Sub mode:").pack(side="left", padx=(0, 2))
        self.submode_var = tk.StringVar(value="USB")
        ttk.Combobox(r2b, textvariable=self.submode_var, width=5, state="readonly",
                     values=MODES).pack(side="left")
        self.submode_var.trace_add("write", self._submode_changed)
        ttk.Label(r2b, text="Sub filter:").pack(side="left", padx=(10, 2))
        self.subfilt_var = tk.StringVar(value="2.7k")
        ttk.Combobox(r2b, textvariable=self.subfilt_var, width=5, state="readonly",
                     values=["5k", "3.8k", "2.9k", "2.7k", "2.4k", "1.8k", "1k", "500", "250"]).pack(side="left")
        self.subfilt_var.trace_add("write", self._subfilt_changed)
        ttk.Label(r2b, text="Audio:").pack(side="left", padx=(12, 2))
        self.audiosel_var = tk.StringVar(value="Main")
        ttk.Combobox(r2b, textvariable=self.audiosel_var, width=6, state="readonly",
                     values=["Main", "Sub", "Both"]).pack(side="left")
        self.audiosel_var.trace_add("write", self._audiosel_changed)
        ttk.Label(r2b, text="A<->B mix:").pack(side="left", padx=(8, 2))
        self.bal_var = tk.DoubleVar(value=1.0)
        ttk.Scale(r2b, from_=0.0, to=1.0, variable=self.bal_var, length=100,
                  command=self._bal_changed).pack(side="left")
        self.sub_btn = ttk.Button(r2b, text="SUB off", width=8, command=self._sub_toggle)
        self.sub_btn.pack(side="left", padx=(10, 0))
        self.split_btn = ttk.Button(r2b, text="SPLIT off", width=9, command=self._split_toggle)
        self.split_btn.pack(side="left", padx=(6, 0))

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
        self.pan.bind("<Double-Button-1>", self._pan_double)   # Branch H1/B7 peak-tune

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
        self.txtail_var = tk.IntVar(value=350)
        self.txtail_entry = ttk.Entry(r4, width=5)
        self.txtail_entry.insert(0, "350")
        self.txtail_entry.pack(side="left")
        self.txtail_entry.bind("<Return>", self._txtail_entry)
        self.txtail_entry.bind("<FocusOut>", self._txtail_entry)
        self.ptt_btn.pack(side="left")
        self.tune_btn = tk.Button(r4, text="TUNE", bg="#f7e6c8", fg=C["fg"], width=8,
                                  font=("Segoe UI", 10, "bold"), command=self.tune_toggle)
        self.tune_btn.pack(side="left", padx=(8, 0))
        ttk.Label(r4, text="Tune drive:", padding=(14, 0, 2, 0)).pack(side="left")
        self.tunedrive_entry = ttk.Entry(r4, width=5)
        self.tunedrive_entry.insert(0, "80")
        self.tunedrive_entry.pack(side="left")
        self.tunedrive_entry.bind("<Return>", self._tunedrive_entry)
        self.tunedrive_entry.bind("<FocusOut>", self._tunedrive_entry)
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
        stereo_buf = np.zeros((CHUNK, 2), dtype=np.float32)
        while True:
            try:
                if self.out_stream is None:
                    time.sleep(0.1)
                    continue
                was_muted = getattr(self, "_muting_now", False)
                muted = (self.ptt
                         or getattr(self, "mox_active", False)
                         or time.time() < getattr(self, "_tx_mute_until", 0.0))
                if muted and not was_muted:
                    self.logprint("monitor muted (TX)")
                elif not muted and was_muted:
                    self.logprint("monitor resumed")
                self._muting_now = muted
                if muted:
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
                        stereo_buf[filled:] = 0.0
                        break
                    blk = self.audio_blocks[0]
                    take = min(len(blk) - self.audio_pos, CHUNK - filled)
                    if take <= 0:
                        self.audio_blocks.popleft()
                        self.audio_pos = 0
                        continue
                    stereo_buf[filled:filled + take] = blk[self.audio_pos:self.audio_pos + take]
                    filled += take
                    self.audio_pos += take
                    if self.audio_pos >= len(blk):
                        self.audio_blocks.popleft()
                        self.audio_pos = 0
                # Branch H1: selection is done server-side (per-channel gain);
                # L and R now carry the same selected mix - play as delivered.
                stereo_buf[:, 0] *= self.volume
                stereo_buf[:, 1] *= self.volume
                try:
                    self.out_stream.write(stereo_buf)
                except Exception:
                    time.sleep(0.05)
            except Exception:
                time.sleep(0.1)

    def _vol_changed(self, v):
        # The server-side slice AF gain is now Thetis-like (0.5), so the slider
        # is a plain output attenuator: 0..100 -> 0..1.2 (+1.6 dB max headroom)
        self.volume = (float(v) / 100.0) * 1.2

    def _txtail_entry(self, *_):
        # read the TEXT the user typed (the IntVar is not linked to the entry)
        try:
            ms = int(float(self.txtail_entry.get()))
        except (ValueError, tk.TclError):
            self.txtail_entry.delete(0, "end")
            self.txtail_entry.insert(0, str(int(self.tx_tail_s * 1000)))
            return
        ms = max(0, min(10000, ms))
        self.tx_tail_s = ms / 1000.0
        self.txtail_entry.delete(0, "end")
        self.txtail_entry.insert(0, str(ms))
        # visible acknowledgment: flash the entry text green + log the value
        try:
            self.txtail_entry.configure(foreground="#1a7f37")
            self.txtail_entry.after(900, lambda: self.txtail_entry.configure(
                foreground="#1a1f29"))
        except tk.TclError:
            pass
        self.logprint(f"TX tail set to {ms} ms")

    def _mic_changed(self, v):
        self.mic_gain = float(v) / 100.0

    # ---------------- TCI callbacks (ws thread) ----------------
    def tci_text(self, d):
        self.text_q.put(d)

    def tci_audio(self, data, rate, chans):
        # Branch H1 fix: keep STEREO (L = main, R = sub after server panning).
        # The old code kept only the left channel, so the subrx (panned right)
        # was silent. Resample both channels; the output stream is stereo and
        # the audio selection applies L/R gains at playback.
        if chans == 2:
            L = data[0::2]
            R = data[1::2]
        else:
            L = R = data
        if rate != OUT_RATE:
            n = int(len(L) * OUT_RATE / rate)
            L = resample(L, n)
            R = resample(R, n)
        self.audio_blocks.append(np.stack([L, R], axis=1))   # shape (n, 2)
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
                    # Tag each block with the DDC centre (hardware centre), NOT VFO A.
                    # Under CTUN A floats inside the DDC (freq_hz != DDC centre), and
                    # the IQ data is still centred on the DDC - tagging with freq_hz
                    # shifts the placement by the offset and empties the opposite edge.
                    ddc = self.pan.data_center_hz if self.pan.data_center_hz else self.freq_hz
                    self._iq_q.put_nowait((block, rate, float(ddc)))
                except Exception:
                    # UI stalled - drop the OLDEST block so new data keeps flowing
                    try:
                        self._iq_q.get_nowait()
                        ddc = self.pan.data_center_hz if self.pan.data_center_hz else self.freq_hz
                        self._iq_q.put_nowait((block, rate, float(ddc)))
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
            if self.tuning:
                # TUNE: generated steady tone instead of the mic
                vals = self._tune_gen()[:vals_needed]
                frame = self.build_tx_audio_frame(vals, rate, chans)
                if self.client and self.client.loop:
                    self.client.send_binary(frame)
                continue
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
            self.send(f"ctun:0,{str(self.ctun_var.get()).lower()};")
            # Branch H1: restore subrx state on connect; default B = A + 2 kHz
            if not self.sub_hz:
                self.sub_hz = self.freq_hz + 2000
            self.send("subrx_state:0;")
            if self.sub_enabled:
                self.send("subrx:0,true;")
                self.send(f"vfo:1,0,{self.sub_hz};")
                self.send(f"sub_mode:0,{self.sub_mode};")
                self.send(f"sub_filter:0,{self.sub_filt[0]},{self.sub_filt[1]};")
                self.send(f"sub_balance:0,{self.bal_var.get():.2f};")
            if self.split:
                self.send("split_enable:0,true;")
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
            if k == "subrx_state" and v:
                p = v.split(",")
                if len(p) >= 7:
                    try:
                        st = p[1].lower() == "true"
                        if not self.sub_enabled and st:
                            self.sub_enabled = True
                        srv_hz = int(float(p[2]))
                        if srv_hz > 0:
                            self.sub_hz = srv_hz       # keep local default if server never set one
                        self.sub_mode = p[3].strip().upper()
                        fl, fh = int(p[4]), int(p[5])
                        if fh > fl:
                            self.sub_filt = (fl, fh)
                        try: self.bal_var.set(float(p[6]))
                        except Exception: pass
                        if self.sub_enabled:
                            self._submode_busy = True
                            try: self.submode_var.set(self.sub_mode)
                            finally: self._submode_busy = False
                        self._sub_refresh_ui()
                    except ValueError:
                        pass
                continue
            if k == "ctun" and v:
                self.logprint(f"CTUN state echo: {v}")
                continue
            if k == "subrx":
                # server echo: subrx:0,<bool>; (v = "<trx>,<bool>")
                p = str(v).split(",")
                st = p[-1].strip().lower() == "true"
                if st and not self.sub_enabled:
                    self.sub_enabled = True
                    self.logprint("SubRX on (VFO B)")
                elif not st and self.sub_enabled:
                    self.sub_enabled = False
                    if self.split:
                        self.split = False
                    self.logprint("SubRX off (refused or released)")
                self._sub_refresh_ui()
                continue
            if k == "split_enable":
                st = str(v).split(",")[-1].strip().lower() == "true"
                self._split_set(st)
                self.logprint(f"SPLIT {'on - TX on VFO B' if st else 'off'}")
                continue
            if k == "vfo" and v:
                p = v.split(",")
                # Branch H1: vfo:1,0,<hz> = VFO B echo
                if len(p) >= 3 and p[0] == "1":
                    try:
                        self.sub_hz = int(float(p[2]))
                        self._sub_refresh_ui()
                    except ValueError:
                        pass
                    continue
            if k == "sub_mode" and v:
                # echo format: sub_mode:<trx>,<MODE>; - take the mode token
                mode_in = str(v).split(",")[-1].strip().upper()
                if mode_in in MODES:
                    self._submode_busy = True
                    try:
                        if mode_in != self.sub_mode:
                            self.logprint(f"Sub mode echo: {mode_in}")
                        self.sub_mode = mode_in
                        self.submode_var.set(mode_in)
                    finally:
                        self._submode_busy = False
                continue
            if k == "sub_filter" and v:
                # echo format: sub_filter:<trx>,<lo>,<hi>; - skip the trx index
                p = str(v).split(",")
                if len(p) >= 3:
                    try:
                        fl, fh = int(p[1]), int(p[2])
                        if fh > fl:
                            self.sub_filt = (fl, fh)
                            self._sub_refresh_ui()
                    except ValueError:
                        pass
                continue
            if k == "probe":
                # Thetis TX-chain probe (once/s while we transmit):
                # q=queued TCI samples in Thetis, calls/samps=pulls into the TX DSP,
                # cyc=TX DSP cycles, tci=1 if TCI is the TX source, in/out=RMS at
                # the WDSP input/output, outdev/outrms=frames+RMS to the TX Out device
                self.logprint("probe " + str(v))
                continue
            if k == "vfo" and v:
                p = v.split(",")
                if len(p) >= 3:
                    try:
                        hz = float(p[2])
                        self.freq_hz = int(hz)
                        self._fmt_freq()
                        self.pan.vfo_hz = hz
                        # Branch H1: data_center_hz belongs to the DDC (dds echo),
                        # NOT to A - under CTUN A floats inside the DDC and must
                        # not drag the data placement with it.
                    except ValueError:
                        pass
            elif k == "dds" and v:
                try:
                    dds_hz = float(v.split(",")[-1])
                    self.ddc_center_hz = int(dds_hz)
                    self.dds_lbl.config(text="DDS " + self._fmt_hz(dds_hz))
                    self.pan.data_center_hz = dds_hz   # actual DDC center of the IQ data
                    if self.pan.center_hz and abs(dds_hz - self.pan.center_hz) > 1:
                        # DDC moved (band change, classic follow, or CTUN scroll):
                        # slide the whole display + waterfall history to stay aligned
                        self.pan.shift_waterfall(dds_hz - self.pan.center_hz)
                        self.pan.center_hz = dds_hz
                    elif not self.pan.center_hz:
                        self.pan.center_hz = dds_hz
                except (ValueError, IndexError):
                    pass
            elif k == "mox" and v:
                # Thetis main-GUI MOX/Tune (any transmitter on site): mute the
                # monitor - the on-site blast overloads the Red Pitaya RX and
                # plays as distorted self-audio.
                mox_on = v.split(",")[-1].lower() == "true"
                if mox_on != getattr(self, "mox_active", False):
                    self.mox_active = mox_on
                    if mox_on:
                        self.logprint("MOX active (Thetis) - monitor muted")
                    else:
                        self._tx_mute_until = time.time() + self.tx_tail_s
                        self.logprint("MOX released - monitor resumes")
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

    def _fmt_hz(self, hz):
        f = max(0, int(hz))
        return f"{f // 1_000_000}.{(f % 1_000_000) // 1000:03d}.{f % 1000:03d}"

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

    # ---------------- Branch H1: VFO B / subrx ----------------
    def _sub_enabled(self):
        return self.connected and self.sub_enabled

    def _fmt_sub_freq(self):
        # same dotted format as VFO A (H1: consistent displays, different colors)
        f = max(0, int(self.sub_hz))
        mhz = f // 1_000_000
        khz = (f % 1_000_000) // 1000
        hz = f % 1000
        return f"{mhz}.{khz:03d}.{hz:03d}"

    def _sub_refresh_ui(self):
        on = self.sub_enabled
        self.vfo_lbl.config(text="VFO B  " + self._fmt_sub_freq())
        self.sub_btn.config(text="SUB on" if on else "SUB off")
        self.split_btn.config(text="SPLIT on" if self.split else "SPLIT off")
        self.pan.sub_hz = self.sub_hz if on else 0.0
        self.pan.sub_filt = self.sub_filt

    def _sub_toggle(self):
        if not self.connected:
            self.logprint("connect first to use SubRX")
            return
        want = not self.sub_enabled
        if want and self.sub_hz == 0:
            self.sub_hz = self.freq_hz + 2000   # default: 2 kHz above VFO A
        self.sub_enabled = want                  # optimistic; server echo confirms
        self._sub_refresh_ui()
        self.send(f"subrx:0,{str(want).lower()};")
        if want:
            self.send(f"vfo:1,0,{self.sub_hz};")
            self.send(f"sub_mode:0,{self.sub_mode};")
            lo, hi = self.sub_filt
            self.send(f"sub_filter:0,{lo},{hi};")
            self.send(f"sub_balance:0,{self.bal_var.get():.2f};")
        # state applied on server echo (subrx handler in _handle)

    def _split_toggle(self):
        if not self.connected:
            self.logprint("connect first to use Split")
            return
        want = not self.split
        if want and not self.sub_enabled:
            self._sub_toggle()                  # split needs the subrx
            if not self.sub_enabled:
                return
        self.send(f"split_enable:0,{str(want).lower()};")
        # state applied on server echo

    def _submode_changed(self, *_a):
        # guard: server echoes re-set the var; sending on echo would loop forever
        if getattr(self, "_submode_busy", False):
            return
        new_mode = self.submode_var.get()
        if new_mode == getattr(self, "sub_mode", None):
            return                      # same value - nothing to do
        self.sub_mode = new_mode
        # the filter must flip to the new sideband convention (negative offsets
        # for LSB-family) - recompute width edges and push after the mode
        w = BW_PRESETS.get(self.subfilt_var.get(), 2700)
        lo_edge = min(100, w // 8)
        if new_mode in ("LSB", "DIGL", "CWL"):
            self.sub_filt = (-w + lo_edge, -lo_edge)
        else:
            self.sub_filt = (lo_edge, w)
        self._sub_refresh_ui()
        if self._sub_enabled():
            self.send(f"sub_mode:0,{self.sub_mode};")
            lo, hi = self.sub_filt
            self.send(f"sub_filter:0,{lo},{hi};")
        self.logprint(f"Sub mode {self.sub_mode} filt {self.sub_filt}")

    def _subfilt_changed(self, *_a):
        # presets are TOTAL widths, same convention as VFO A
        w = BW_PRESETS.get(self.subfilt_var.get(), 2700)
        lo_edge = min(100, w // 8)
        if self.sub_mode in ("LSB", "DIGL", "CWL"):
            self.sub_filt = (-w + lo_edge, -lo_edge)
        else:
            self.sub_filt = (lo_edge, w)
        self._sub_refresh_ui()
        if self._sub_enabled():
            lo, hi = self.sub_filt
            self.send(f"sub_filter:0,{lo},{hi};")

    def _audiosel_changed(self, *_a):
        self.audio_sel = self.audiosel_var.get().lower()
        self._apply_audio_selection()

    def _apply_audio_selection(self, force=False):
        """Branch H1: audio selection via server-side per-channel gain
        (balance 0 = main only, 0.5 = equal mix, 1 = sub only). The pan law
        was removed server-side so both VFOs run at identical gain - selection
        is by muting, not by ear placement."""
        if not self.connected:
            return
        sel = self.audio_sel
        target = {"main": 0.0, "sub": 1.0, "both": None}.get(sel)
        if sel == "both":
            val = self.bal_var.get()
        elif target is not None:
            val = target
        else:
            val = 0.5
        if force or getattr(self, "_last_audio_sel", None) != sel or sel == "both":
            self.send(f"sub_balance:0,{val:.2f};")
        self._last_audio_sel = sel
        self.logprint(f"audio: {sel} (balance {val:.2f})")

    def _bal_changed(self, v):
        # balance only actively drives placement in 'Both' mode; Main/Sub force
        # hard L/R panning for clean isolation
        if self._sub_enabled() and self.audio_sel == "both":
            self.send(f"sub_balance:0,{float(v):.2f};")

    def _sub_wheel(self, e):
        if not self.sub_enabled:
            return
        # same steps as VFO A: 100 Hz, Shift = 10 Hz fine
        step = 10 if e.state & 0x0001 else 100
        d = step if getattr(e, "delta", 120) > 0 else -step
        self._sub_tune_to(max(0, self.sub_hz + d))

    def _sub_tune_direct(self):
        t = self.sub_tune_entry.get().strip().replace(",", ".")
        try:
            mhz = float(t) if "." in t else float(t) / 1000.0
            self._sub_tune_to(int(mhz * 1e6))
            self.logprint(f"B -> {self._fmt_sub_freq()}")
        except ValueError:
            self.logprint("invalid B frequency")

    def _sub_tune_to(self, hz):
        self.sub_hz = int(hz)
        self._clamp_sub_to_ddc()
        self._sub_refresh_ui()
        if self._sub_enabled():
            self.send(f"vfo:1,0,{self.sub_hz};")

    def _split_set(self, on):
        self.split = on
        if on:
            self.tx_vfo = "B"
        else:
            self.tx_vfo = "A"
        self._sub_refresh_ui()

    # ---------------- Branch H1/B7: scenario buttons ----------------
    def _scene_capture(self):
        return {
            "a": self.freq_hz, "mode": self.mode_var.get(),
            "b": self.sub_hz, "sub_mode": self.submode_var.get(),
            "sub_on": self.sub_enabled, "split": self.split,
            "agc": self.agc_var.get(),
        }

    def _scene_store(self):
        if not hasattr(self, "_scenes"):
            self._scenes = {}
        # store into the last-recalled (or first) slot
        idx = getattr(self, "_scene_last", 0)
        data = self._scene_capture()
        data["name"] = data["mode"] + " " + f"{data['a']/1e6:.3f}"
        self._scenes[str(idx)] = data
        self.scene_btns[idx].config(text=data["name"][:14])
        self._save_settings()
        self.logprint(f"scene {idx + 1} stored: {data['name']}")

    def _scene_recall(self, idx):
        self._scene_last = idx
        sc = getattr(self, "_scenes", {}).get(str(idx))
        if not sc:
            self.logprint(f"scene {idx + 1} is empty - press Store to capture the current state")
            return
        self.band_var.set(self._band_for_freq(sc["a"]) or self.band_var.get())
        self._apply_state(sc)
        self.logprint(f"scene {idx + 1}: A={sc['a']/1e6:.3f} {sc['mode']}  B={(sc.get('b') or 0)/1e6:.3f} {sc.get('sub_mode','')}  sub={'on' if sc.get('sub_on') else 'off'} split={'on' if sc.get('split') else 'off'}")

    def _band_for_freq(self, hz):
        for b in BANDS:
            if b[1] <= hz / 1e6 < b[1] + 2.0:
                return b[0]
        return None

    FT8_SEGMENTS_MHZ = (1.840, 3.573, 7.074, 10.136, 14.074, 18.100, 21.074, 24.915, 28.074)

    def _digital_segment_hint(self):
        f = self.freq_hz / 1e6
        for seg in self.FT8_SEGMENTS_MHZ:
            if abs(f - seg) <= 0.003:
                return seg
        return None

    def _ctun_toggled(self):
        # Branch H1: tell the server which display model is in use
        self.send(f"ctun:0,{str(self.ctun_var.get()).lower()};")
        self.logprint(f"CTUN {'on' if self.ctun_var.get() else 'off'}")

    def _clamp_sub_to_ddc(self):
        """Branch H1: the VFO B tuning LINE may reach the full DDC passband
        (+/-48 kHz around A) in every mode - the user tunes to the visible
        waterfall edge. Where the filter geometry would extend past the edge,
        the passband colour simply clips at the edge (partially demodulated,
        as in Thetis at rate/2)."""
        if not self.sub_hz:
            return
        off = self.sub_hz - self.freq_hz
        edge = 48000
        if abs(off) > edge:
            self.sub_hz = self.freq_hz + (edge if off > 0 else -edge)

    def tune_to(self, hz):
        self.freq_hz = int(hz)
        self._fmt_freq()
        # keep VFO B glued to the band/DDC: re-place it relative to the new A
        if self._sub_enabled():
            self._clamp_sub_to_ddc()
            self.send(f"vfo:1,0,{self.sub_hz};")
            self._sub_refresh_ui()
        seg = self._digital_segment_hint()
        if seg and self.mode_var.get() in ("USB", "LSB"):
            # B7-4: entering an FT8 segment in SSB - suggest the digital mode
            self.logprint(f"FT8 segment {seg:.3f} MHz: consider mode DIGU")
        # Branch H1: the display centre follows the DDC (dds echo from the server).
        # Non-CTUN: server re-centres DDC onto A and echoes dds -> display slides.
        # CTUN: A floats inside the DDC (server-side RXOsc); display stays fixed,
        # only the tune line moves. The client never self-shifts here.
        self.pan.vfo_hz = self.freq_hz
        self.send(f"vfo:0,0,{self.freq_hz};")

    def _tune_direct(self):
        t = self.tune_entry.get().strip().replace(",", ".")
        try:
            mhz = float(t) if "." in t else float(t) / 1000.0
            self.tune_to(int(mhz * 1e6))
        except ValueError:
            self.logprint("bad frequency (use MHz like 14.074, or kHz)")

    # Branch H1 / B7: band stack - each band remembers its full state
    def _band_stack_key(self):
        return self.band_var.get()

    def _band_stack_save(self):
        """Store the current state under the CURRENT band before leaving it."""
        if not hasattr(self, "_band_stacks"):
            self._band_stacks = {}
        key = getattr(self, "_band_stack_current", None)
        if key:
            self._band_stacks[key] = {
                "a": self.freq_hz, "mode": self.mode,
                "filt": self.filtwidth_var.get(),
                "b": self.sub_hz, "sub_mode": self.sub_mode,
                "sub_filt": self.subfilt_var.get(),
                "sub_on": self.sub_enabled, "split": self.split,
            }

    def _band_stack_load(self, key):
        st = getattr(self, "_band_stacks", {}).get(key)
        if not st:
            return False
        self._apply_state(st)
        return True

    def _apply_state(self, st):
        """Restore a full operating state (VFO A + B, modes, sub, split)."""
        self.mode_var.set(st["mode"])
        if st.get("filt"):
            self.filtwidth_var.set(st["filt"])
        if st.get("sub_filt"):
            self.subfilt_var.set(st["sub_filt"])
        self.tune_to(int(st["a"]))
        self.sub_hz = int(st.get("b") or 0)
        self.sub_mode = st.get("sub_mode", "USB")
        self.submode_var.set(self.sub_mode)
        want_sub = bool(st.get("sub_on"))
        if want_sub and self.connected:
            self.send(f"subrx:0,true;")
            self.sub_enabled = True
            self.send(f"vfo:1,0,{self.sub_hz};")
            self.send(f"sub_mode:0,{self.sub_mode};")
            self.send(f"sub_filter:0,{self.sub_filt[0]},{self.sub_filt[1]};")
        elif self.sub_enabled:
            self.send("subrx:0,false;")
            self.sub_enabled = False
        want_split = bool(st.get("split")) and want_sub
        if self.connected:
            self.send(f"split_enable:0,{str(want_split).lower()};")
        self.split = want_split
        self._sub_refresh_ui()

    def _band_changed(self, *_):
        name = self.band_var.get()
        prev = getattr(self, "_band_stack_current", None)
        if prev and prev != name:
            self._band_stack_save()
        self._band_stack_current = name
        if self._band_stack_load(name):
            return
        for b in BANDS:
            if b[0] == name:
                self.tune_to(int(b[1] * 1e6))
                self.mode_var.set(b[2])
                # no stored stack for this band: put B near A in the new band
                if self.sub_enabled and self._band_for_freq(self.sub_hz) != name:
                    self.sub_hz = self.freq_hz + 2000
                    self._sub_tune_to(self.sub_hz)
                return

    def _mode_changed(self, *_):
        self.mode = self.mode_var.get()
        lo, hi = FILTERS.get(self.mode, (100, 2900))
        self.pan.filt = (lo, hi)
        self.send(f"modulation:0,{self.mode};")
        self.send(f"rx_filter_band:0,{lo},{hi};")

    def _filtwidth_changed(self, *_):
        # presets are TOTAL filter widths (Hz): e.g. 2.7k -> 100..2800
        w = BW_PRESETS.get(self.filtwidth_var.get(), 2900)
        lo_edge = min(100, w // 8)
        mode = self.mode_var.get()
        if mode in ("LSB", "DIGL", "CWL"):
            lo, hi = -w + lo_edge, -lo_edge
        else:
            lo, hi = lo_edge, w
        self.pan.filt = (lo, hi)
        if self.connected:
            self.send(f"rx_filter_band:0,{lo},{hi};")
        self.logprint(f"VFO A filter {self.filtwidth_var.get()} ({lo}..{hi} Hz)")

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

    def _pan_double(self, e):
        """Branch H1/B7: double-click = tune the SELECTED VFO to the spectral
        peak nearest the click (quadratic interpolation around the max bin),
        WSJT-X-style click-tuning. Left of the X axis strip only."""
        if e.y > PAN_H or not getattr(self.pan, "_last_db", None) is not None:
            return
        if not self.pan.center_hz:
            return
        db = self.pan._last_db
        n = len(db)
        f_click = self.pan.x2f(e.x)
        half_win = 1500.0
        i_lo = int((f_click - half_win) / self.pan.rate * n) + n // 2
        i_hi = int((f_click + half_win) / self.pan.rate * n) + n // 2
        i_lo, i_hi = max(0, i_lo), min(n - 1, i_hi)
        if i_hi <= i_lo + 2:
            return
        k = i_lo + int(np.argmax(db[i_lo:i_hi]))
        if k <= 0 or k >= n - 1:
            return
        # quadratic interpolation on the three bins around the peak
        a, b, c = db[k - 1], db[k], db[k + 1]
        denom = (a - 2 * b + c)
        dk = 0.5 * (a - c) / denom if abs(denom) > 1e-9 else 0.0
        dk = max(-0.5, min(0.5, dk))
        f_peak = (k + dk - n / 2) / n * self.pan.rate + self.pan.data_center_hz
        if self._tx_vfo_selected() == "B":
            self._sub_tune_to(int(round(f_peak)))
            self.logprint(f"B -> {self._fmt_sub_freq()} (peak)")
        else:
            self.tune_to(int(round(f_peak)))
            self.logprint(f"A -> {f_peak:,.0f} Hz (peak)")

    def _tx_vfo_selected(self):
        return self.tx_vfo

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

    # ---------------- tune ----------------
    def _tunedrive_entry(self, *_):
        try:
            pct = float(self.tunedrive_entry.get())
        except ValueError:
            self.tunedrive_entry.delete(0, "end")
            self.tunedrive_entry.insert(0, str(int(self.tune_amp / 0.25 * 100)))
            return
        pct = max(1, min(100, int(pct)))
        self.tune_amp = 0.25 * pct / 100.0
        self.tunedrive_entry.delete(0, "end")
        self.tunedrive_entry.insert(0, str(pct))
        self.logprint(f"Tune drive set to {pct}% (peak {20*np.log10(self.tune_amp):.1f} dBFS)")

    def tune_toggle(self):
        """WSJT-X-style Tune: press to start, press again to stop. Transmits a
        steady single tone at the configured drive level (Tune drive entry),
        full RF power without saturating the Thetis TX chain."""
        if not self.connected:
            self.logprint("connect first to use Tune")
            return
        if self.tuning:
            self.ptt_off()            # ptt_off clears self.tuning + button look
            self.logprint("Tune OFF")
            return
        if self.ptt:
            self.logprint("PTT active - Tune not available")
            return
        self.tuning = True
        self.tune_phase = 0.0
        self.tune_sample_pos = 0
        self.ptt = True
        self.send("tx_stream_audio_buffering:100;")
        self.send("audio_stream_sample_type:float32;")
        self.send("audio_stream_channels:1;")
        self.send("audio_stream_samples:1024;")
        self.send("trx:0,true;")
        self.tune_btn.config(bg=C["red"], relief="sunken")
        self.tx_lbl.config(text="TX \u23fa tune", fg=C["red"])
        self.logprint(f"Tune ON: 1500 Hz tone, drive {self.tune_amp:.3f} peak")

    def tune_stop(self):
        if not self.tuning:
            return
        self.tuning = False
        self.ptt_off()

    def _tune_gen(self):
        """Generate the next tune tone block (called instead of the mic while
        tuning). Steady 1500 Hz sine; peak amplitude 0.2 = -14 dBFS - full
        drive for digital modes without saturating the TX chain."""
        n = 1024
        ph = self.tune_phase
        amp = self.tune_amp
        t = (np.arange(n) + self.tune_sample_pos) / TX_AUDIO_RATE
        self.tune_sample_pos += n
        self.tune_phase = (self.tune_phase + 2 * np.pi * 1500.0 * n / TX_AUDIO_RATE) % (2 * np.pi)
        tone = (amp * np.sin(2 * np.pi * 1500.0 * t)).astype(np.float32)
        # drive the S-meter with the tone level exactly like the mic path does
        # (RMS of the block in dBFS); a sine of peak A has RMS A/sqrt(2)
        self.mic_level_db = 20.0 * np.log10(float(amp) / np.sqrt(2.0) + 1e-10)
        return tone

    def ptt_off(self):
        if not self.ptt:
            return
        self.ptt = False
        # fixed minimum tail, then playback resumes (the box controls the tail)
        self._tx_mute_until = time.time() + self.tx_tail_s
        self.mic_level_db = -140.0
        self.send("trx:0,false;")
        self.ptt_btn.config(bg="#e3b8b3", relief="raised")
        self.tx_lbl.config(text="RX", fg=C["dim"])
        if getattr(self, "tuning", False):
            self.tuning = False
            self.tune_btn.config(bg="#f7e6c8", relief="raised")
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
            stamp = time.strftime("%H:%M:%S")
            self.log.insert("end", f"[{stamp}] {s}\n")
            self.log.see("end")
            if float(self.log.index("end-1c").split(".")[0]) > 60:
                self.log.delete("1.0", "20.0")
        except Exception:
            pass


if __name__ == "__main__":
    MiniTCI().mainloop()