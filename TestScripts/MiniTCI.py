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
import tkinter.font as tkfont
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
MIC_MIN = -40          # Thetis mic gain range (mic_gain_min / mic_gain_max)
MIC_MAX = 10
TUNE_TONE_AMP = 0.2    # fixed tune tone, -14 dBFS (WSJT-X drive convention)
TX_AUDIO_RATE = 48000   # TX audio stream rate (negotiated with the server)

BANDS = [  # name, default MHz, suggested mode
    ("160m", 1.850, "LSB"),
    ("80m",  3.600, "LSB"),
    ("60m",  5.3585, "USB"),
    ("40m",  7.060, "LSB"),
    ("30m", 10.116, "CWU"),
    ("20m", 14.074, "USB"),
    ("17m", 18.100, "USB"),
    ("15m", 21.074, "USB"),
    ("12m", 24.920, "USB"),
    ("10m", 28.400, "USB"),
    ("6m",  50.313, "USB"),
    ("2m", 144.200, "FM"),
    ("70cm", 432.200, "FM"),
]

# Non-overlapping frequency ranges per band (lo MHz, hi MHz). 60m is a narrow,
# region-dependent allocation (UK 5.2585-5.4065, US 5.332-5.405, EU 5.3515-5.3665);
# the wide window covers all three without spilling into 80m or 40m.
BAND_RANGES = {
    "160m": (1.800, 2.000),
    "80m":  (3.500, 3.800),
    "60m":  (5.3515, 5.3665),
    "40m":  (7.000, 7.300),
    "30m":  (10.100, 10.150),
    "20m":  (14.000, 14.350),
    "17m":  (18.068, 18.168),
    "15m":  (21.000, 21.450),
    "12m":  (24.890, 24.990),
    "10m":  (28.000, 29.700),
    "6m":   (50.000, 54.000),
    "2m":   (144.000, 148.000),
    "70cm": (430.000, 440.000),
}

# Thetis DSPMode order/names (enums.cs), restricted to what the TCI
# modulation handler accepts on 50001 (lsb usb dsb am sam fm cw cwl cwu digl digu)
MODES = ["LSB", "USB", "DSB", "CWL", "CWU", "FM", "AM", "DIGU", "DIGL", "SAM", "DRM", "SPEC"]
# Branch H1: filter presets aligned with Thetis (console.cs InitFilterPresets).
# SSB/DIGL-family: Thetis F1..F10 (label, low, high) - low edge 100 Hz from
# carrier, high = width. DIGU/DIGL centre on the click-tune offset (1500/2210).
# CW centres on cw_pitch (600). AM/SAM symmetric. Values are Thetis's F-button
# table verbatim so MiniTCI's width dropdown matches Thetis's F-buttons.

THETIS_SSB = [("5.0k", 5100), ("4.4k", 4500), ("3.8k", 3900), ("3.3k", 3400),
              ("2.9k", 3000), ("2.7k", 2800), ("2.4k", 2500), ("2.1k", 2200),
              ("1.8k", 1900), ("1.0k", 1100)]          # (label, hi-edge), lo=100

THETIS_DIG = [("3.0k", 1500), ("2.5k", 1250), ("2.0k", 1000), ("1.5k", 750),
              ("1.0k", 500), ("800", 400), ("600", 300), ("300", 150),
              ("150", 75), ("75", 38)]                  # half-widths around offset

THETIS_CW = [("1.0k", 500), ("800", 400), ("600", 300), ("500", 250),
             ("400", 200), ("250", 125), ("150", 75), ("100", 50),
             ("50", 25), ("25", 13)]                    # half-widths around pitch

THETIS_AM = [("20k", 10000), ("18k", 9000), ("16k", 8000), ("12k", 6000),
             ("10k", 5000), ("9.0k", 4500), ("8.0k", 4000), ("7.0k", 3500),
             ("6.0k", 3000), ("5.0k", 2500)]            # half-widths, symmetric

DIGU_OFFSET = 1500    # Thetis digu_click_tune_offset default
DIGL_OFFSET = 2210    # Thetis digl_click_tune_offset default
CW_PITCH = 600        # Thetis cw_pitch default

def _thetis_filter(mode, idx):
    """(low, high) for mode's idx-th preset (0-based, idx 4 = F5 default)."""
    if mode == "USB":
        return (100, THETIS_SSB[idx][1])
    if mode == "LSB":
        return (-THETIS_SSB[idx][1], -100)
    if mode == "DIGU":
        d = DIGU_OFFSET
        return (d - THETIS_DIG[idx][1], d + THETIS_DIG[idx][1])
    if mode == "DIGL":
        d = DIGL_OFFSET
        return (-d - THETIS_DIG[idx][1], -d + THETIS_DIG[idx][1])
    if mode == "CWU":
        return (CW_PITCH - THETIS_CW[idx][1], CW_PITCH + THETIS_CW[idx][1])
    if mode == "CWL":
        return (-CW_PITCH - THETIS_CW[idx][1], -CW_PITCH + THETIS_CW[idx][1])
    # AM / SAM symmetric (Thetis AM table); FM follows the deviation
    if mode == "FM":
        # 5k deviation -> ~7.5k half-width, 2.5k -> ~3.75k (Thetis FM)
        half = 7500 if idx < 5 else 3750
        return (-half, half)
    if mode in ("AM", "SAM"):
        half = THETIS_AM[idx][1]
        return (-half, half)
    if mode == "DRM":
        # DRM occupies +/-5 kHz around the dial, same window as AM. Thetis
        # internally applies 7000..17000 relative to its -12 kHz-shifted DDS,
        # which is the same physical passband; the client shows it in the
        # dial-relative frame.
        return (-5000, 5000)
    if mode == "SPEC":
        # Thetis SPEC: filters disabled, SpectrumPreFilter = full band.
        # The client draws the full DDC span; the server does not filter.
        return None
    return (100, 3000)

# width dropdown: Thetis F-button labels for the widest table (SSB), shared
# across modes; index selects the row in each mode's table
BW_PRESETS = [t[0] for t in THETIS_SSB]

# Thetis comboAGC order and casing - used by BOTH the VFO A and the SubVFOA
# AGC dropdowns. Each receiver has its own AGC (WDSP channel per VFO).
AGC_UI_NAMES = ["Fixed", "Long", "Slow", "Med", "Fast", "Custom"]

def _filter_for_mode_width(mode, width_label):
    try:
        idx = BW_PRESETS.index(width_label)
    except ValueError:
        idx = 4
    return _thetis_filter(mode, idx)

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

def smeter_units(dbm):
    """Thetis's own dBm to S-unit mapping, Common.SMeterFromDBM.

    S9 spans -96 to -90 dBm, S9+20 spans -76 to -66, and each S-unit below S9 is
    6 dB, so S1 is -144 to -138 and S0 is -144 and below. Mirrored exactly so the
    two applications label the same signal the same way.
    """
    if dbm is None:
        return "idle"
    if dbm <= -144.0:
        return "S0"
    if dbm <= -138.0:
        return "S1"
    for n in range(2, 10):
        lo = -144.0 + (n - 1) * 6.0
        if dbm <= lo + 6.0:
            return f"S{n}"
    if dbm <= -86.0:
        return "S9+5"
    if dbm <= -80.0:
        return "S9+10"
    if dbm <= -76.0:
        return "S9+15"
    if dbm <= -66.0:
        return "S9+20"
    if dbm <= -56.0:
        return "S9+30"
    if dbm <= -46.0:
        return "S9+40"
    return "S9+50"


class Smeter(tk.Canvas):
    """Analog S-meter in Thetis's units, one instance per DSP channel.

    Thetis meters every DSP channel separately (WDSP RXA_S_PK / RXA_S_AV), so VFO
    A and the sub each get their own bar and show the same numbers Thetis shows.
    The scale is Thetis's: the bar runs from -147 dBm at the left to -43 dBm at
    the right, S9 sits at -93 dBm and one S-unit is 6 dB.
    """

    DB_FLOOR = -147.0        # left end, S0 and below
    DB_CEIL = -43.0          # right end, S9+50
    S9_DBM = -93.0
    P20_DBM = -69.0          # Thetis S9+20

    def __init__(self, master, width=330, height=64, label="S"):
        super().__init__(master, width=width, height=height, bg=C["panel"],
                         highlightthickness=0)
        self._x0, self._x1, self._y = 8, width - 8, 24
        self._label = label
        self._peak_db = None
        self._s9 = self.f2x(self.S9_DBM)
        self._p20 = self.f2x(self.P20_DBM)

        def _blend(rgb, a):
            w = (0xa8, 0xd8, 0xea)                      # water tint #a8d8ea
            return "#%02x%02x%02x" % tuple(int(c * (1.0 - a) + w[i] * a)
                                           for i, c in enumerate(rgb))

        y = self._y
        self.create_rectangle(self._x0, y - 11, self._s9, y, fill="#3fa34d", outline="")
        self.create_rectangle(self._s9, y - 11, self._p20, y, fill="#e0a63a", outline="")
        self.create_rectangle(self._p20, y - 11, self._x1, y, fill="#c0392b", outline="")
        self.create_rectangle(self._x0, y - 11, self._x1, y, fill="", outline="#8a8a8a")
        self._fill_g = self.create_rectangle(self._x0, y - 11, self._x0, y,
                                            fill=_blend((0x3f, 0xa3, 0x4d), 0.5), outline="")
        self._fill_a = self.create_rectangle(self._x0, y - 11, self._x0, y,
                                            fill=_blend((0xe0, 0xa6, 0x3a), 0.5), outline="")
        self._fill_r = self.create_rectangle(self._x0, y - 11, self._x0, y,
                                            fill=_blend((0xc0, 0x39, 0x2b), 0.5), outline="")
        self._needle = self.create_line(self._x0, y - 11, self._x0, y,
                                        fill="#1a1f29", width=2)
        self._peak = self.create_line(self._x0, y - 11, self._x0, y,
                                      fill="#ffffff", width=3)
        # S1..S9 at 6 dB steps, then the Thetis +10 and +20 marks
        for n in range(1, 10):
            x = self.f2x(-144.0 + n * 6.0)
            self.create_line(x, y - 11, x, y - 16, fill="#444444")
            self.create_text(x, y - 23, text=str(n), fill="#333333",
                             font=("Segoe UI", 8, "bold"))
        for dbm, lab in ((-79.0, "+10"), (-69.0, "+20")):
            x = self.f2x(dbm)
            self.create_line(x, y - 11, x, y - 16, fill="#444444")
            self.create_text(x, y - 23, text=lab, fill="#8a4a10",
                             font=("Segoe UI", 8, "bold"))
        self.create_text(self._x0 - 2, y - 18, text=label, fill="#444444",
                         font=("Segoe UI", 7, "bold"))
        self._txt = self.create_text(self._x1, height - 6, text="idle", anchor="e",
                                     fill=C["fg"], font=("Consolas", 11, "bold"))

    def f2x(self, dbm):
        frac = clamp((dbm - self.DB_FLOOR) / (self.DB_CEIL - self.DB_FLOOR), 0.0, 1.0)
        return self._x0 + frac * (self._x1 - self._x0)

    def set_level(self, dbm):
        """dbm = signal level in dBm, or None for idle (no measurement)."""
        if dbm is None:
            self._peak_db = None
            self.coords(self._needle, self._x0, self._y - 11, self._x0, self._y)
            self.coords(self._peak, self._x0, self._y - 11, self._x0, self._y)
            for fill in (self._fill_g, self._fill_a, self._fill_r):
                self.coords(fill, self._x0, self._y - 11, self._x0, self._y)
            self.itemconfig(self._txt, text=f"{self._label} idle" if self._label else "idle",
                            fill=C["dim"])
            return
        db = clamp(float(dbm), self.DB_FLOOR, 0.0)
        x = self.f2x(db)
        y_top, y_bot = self._y - 11, self._y
        # translucent "water" fill rises with the signal, zone colours show through
        xg = min(x, self._s9)
        self.coords(self._fill_g, self._x0, y_top, xg, y_bot)
        if x > self._s9:
            self.coords(self._fill_a, self._s9, y_top, min(x, self._p20), y_bot)
        else:
            self.coords(self._fill_a, self._s9, y_top, self._s9, y_bot)
        if x > self._p20:
            self.coords(self._fill_r, self._p20, y_top, x, y_bot)
        else:
            self.coords(self._fill_r, self._p20, y_top, self._p20, y_bot)
        self.coords(self._needle, x, y_top, x, y_bot)
        # peak hold: rises instantly, decays slowly
        pk = db if self._peak_db is None else max(db, self._peak_db - 0.4)
        self._peak_db = clamp(pk, self.DB_FLOOR, 0.0)
        px = self.f2x(self._peak_db)
        self.coords(self._peak, px, y_top, px, y_bot)
        self.itemconfig(self._txt, text=f"{smeter_units(db)}  {db:.0f} dBm",
                        fill=C["fg"])


class MiniTCI(tk.Tk):
    # H1: MiniTCI's AGC names <-> Thetis TCI agc_mode tokens. These mirror
    # TCIServer's agcModeToTciMode/tciModeToAgcMode EXACTLY (FIXD = "off",
    # MED = "normal") - they are the contract for the bi-directional sync.
    AGC_MODES_TCI = {"Fixed": "off", "Long": "long", "Slow": "slow",
                     "Med": "normal", "Fast": "fast", "Custom": "custom"}
    AGC_MODES_FROM_TCI = {"off": "Fixed", "fixd": "Fixed", "fixed": "Fixed",
                          "long": "Long", "slow": "Slow",
                          "normal": "Med", "med": "Med", "medium": "Med",
                          "fast": "Fast", "custom": "Custom"}

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
        self._agc_busy = False            # H1: guard against echo loops
        self._agc_gain_busy = False
        self._mode_busy = False           # H1: modulation echo guard
        self._submode_busy = False        # SubVFOA mode echo guard
        self._sub_agc_busy = False        # SubVFOA AGC echo guards
        self._sub_agc_gain_busy = False
        self._sub_place = 1000            # place under the pointer, sub readout
        self.ddc_center_hz = self.freq_hz  # hardware centre frequency (DDS)
        self.volume = 0.25
        self.mic_gain = 1.0            # client-side unity; Thetis applies the mic gain
        self.dexp_threshold = -40      # console gate threshold we keep untouched
        self._vox_keyed = False        # VOX owns the current transmission
        self._vox_above_at = 0.0
        self.smeter = -140.0
        self.rx_meter_levels = {}      # DSP channel -> dBm, from rx_channel_sensors
        self.tx_mic_dbm = None         # Thetis's own microphone reading, TX only
        self.tx_tail_s = 0.35
        self.tuning = False
        self.tune_active = False      # Thetis's TUN state
        self._key_false_since = None  # when the server last reported RX while keyed
        self._key_requested = False   # local intent: we asked the server to key
        self.tune_phase = 0.0
        self.tune_sample_pos = 0
        self.tune_amp = TUNE_TONE_AMP
        self.tune_drive_pct = 30      # console transmit power during Tune
        self.mic_stream = None
        self.tx_audio_q = collections.deque(maxlen=64)
        self.chrono_reqs = collections.deque()
        self.chrono_lock = threading.Lock()
        self.mic_lock = threading.Lock()
        self.mic_blocks = 0            # mic callbacks since PTT
        self.tx_underruns = 0          # chronos answered without enough mic data
        self._tx_audio_sent = 0
        self._tx_prefilled = False     # mic prebuffer reached for this transmission
        self.mic_rate = None           # rate the mic device really opened at
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
        self.logprint(f"--- MiniTCI session {time.strftime('%Y-%m-%d %H:%M:%S')} "
                      f"(local; the server's TCI log runs on UTC) ---")
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
            "filt": [int(self.pan.filt[0]), int(self.pan.filt[1])] if getattr(self.pan, "filt", None) else None,
            "band": self.band_var.get(),
            "sub_hz": self.sub_hz,
            "sub_mode": self.sub_mode,
            "sub_filt": [self.sub_filt[0], self.sub_filt[1]],
            "sub_filtw": self.sub_filtw_var.get(),
            "sub_agc_mode": self.sub_agc_var.get(),
            "sub_agc_gain": self.sub_agc_gain_var.get(),
            "balance": self.bal_var.get(),
            "sub_enabled": self.sub_enabled,
            "split": self.split,
            "audio_sel": self.audio_sel,
            "volume": self.vol_var.get(),
            "mic_gain": 1.0,          # client-side gain fixed; Thetis applies the mic gain
            "agc_mode": self.agc_var.get(),
            "agc_gain": self.agc_gain_var.get(),
            "y_zero": self.yzero_var.get(),
            "y_scale": self.yscale_var.get(),
            "zoom": self.zoom_var.get(),
            "wf_gain": self.wf_gain_var.get(),
            "ctun": self.ctun_var.get(),
            "txdsp": {k: int(round(float(v["var"].get()))) for k, v in self.txdsp.items()},
            "dexp_on": bool(self.dexp_on),
            "tx_tail_ms": int(self.tx_tail_s * 1000),
            "tune_drive": int(self.tune_drive_pct),
            "out_dev": self.out_dev_var.get(),
            "in_dev": self.in_dev_var.get(),
            "host": getattr(self, "host_var", None).get() if hasattr(self, "host_var") else None,
            "band_stacks": getattr(self, "_band_stacks", {}),
            "scenes": getattr(self, "_scenes", {}),
        }

    def _save_settings(self, *_):
        try:
            # keep the current band's stack fresh so returning to it later works
            self._band_stack_save()
        except Exception:
            pass
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
                    val = s[key]
                    if key == "mode":
                        # migrate legacy names (NFM->FM); drop unknown modes
                        val = {"NFM": "FM"}.get(val, val)
                        if val not in MODES:
                            val = "USB"
                    elif key == "agc_mode":
                        # migrate OFF->Fixed (Thetis FIXD); drop unknown
                        val = {"OFF": "Fixed", "FIXED": "Fixed",
                               "MED": "Med", "FAST": "Fast",
                               "SLOW": "Slow", "LONG": "Long"}.get(val, val)
                        if val not in ("Fixed", "Long", "Slow", "Med", "Fast", "Custom"):
                            val = "Med"
                    var.set(val)
            for key, var in (("volume", self.vol_var),
                             ("agc_gain", self.agc_gain_var), ("y_zero", self.yzero_var),
                             ("y_scale", self.yscale_var), ("zoom", self.zoom_var),
                             ("wf_gain", self.wf_gain_var)):
                if s.get(key) is not None:
                    var.set(float(s[key]))
            # agc_auto checkbox removed (AGC state = mode dropdown)
            if s.get("ctun") is not None:
                self.ctun_var.set(bool(s["ctun"]))
            if s.get("dexp_on") is not None:
                self._dexp_set(bool(s["dexp_on"]))
            for key, val in (s.get("txdsp") or {}).items():
                if key in getattr(self, "txdsp", {}):
                    try:
                        self._txdsp_set(key, int(val))
                    except (ValueError, TypeError):
                        pass
            if s.get("tune_drive") is not None:
                self._apply_tune_drive(int(s["tune_drive"]))
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
            if s.get("filt"):
                try:
                    fl, fh = int(s["filt"][0]), int(s["filt"][1])
                    if fh - fl >= 200:
                        self.pan.filt = (float(fl), float(fh))
                        self._filter_entries_set(self.pan.filt)
                except (ValueError, TypeError, IndexError):
                    pass
            # H1: DRM and SPEC have FIXED filters (set by Thetis SetRX1Mode).
            # A persisted edge pair for them is always stale - it was written by
            # an older build with different defaults (a file carrying DRM
            # 7000..10000 restored +10000 and the user saw the wrong DRM window).
            # Force the mode's definition instead of trusting the file.
            try:
                fixed = _thetis_filter(self.mode_var.get(), 4)
                if self.mode_var.get() == "DRM" or fixed is None:
                    self.pan.filt = fixed if fixed is not None else (-48000.0, 48000.0)
                    self._filter_entries_set(self.pan.filt)
                    self._sync_filtw_slider(self.pan.filt)
            except (ValueError, TypeError, AttributeError):
                pass
            if s.get("sub_agc_mode"):
                val = {"OFF": "Fixed", "FIXED": "Fixed", "MED": "Med", "FAST": "Fast",
                       "SLOW": "Slow", "LONG": "Long"}.get(str(s["sub_agc_mode"]).upper(),
                                                           s["sub_agc_mode"])
                if val in AGC_UI_NAMES:
                    self._sub_agc_busy = True
                    try:
                        self.sub_agc_var.set(val)
                    finally:
                        self._sub_agc_busy = False
            if s.get("sub_agc_gain") is not None:
                self.sub_agc_gain_var.set(float(s["sub_agc_gain"]))
                self._update_sub_agc_gain_label()
            # the sub's passband: edges are the truth, the slider follows
            self._sub_filter_entries_set(self.sub_filt)
            self._sub_sync_filtw_slider(self.sub_filt)
            self.pan.sub_filt = self.sub_filt
            if s.get("sub_enabled") is not None:
                self.sub_enabled = bool(s["sub_enabled"])
            if s.get("split") is not None:
                self.split = bool(s["split"])
            if s.get("audio_sel"):
                self.audio_sel = s["audio_sel"].lower()
                try:
                    self.audiosel_var.set(self.audio_sel.capitalize())
                except (tk.TclError, AttributeError):
                    pass
            self._band_stacks = s.get("band_stacks") or {}
            self._scenes = s.get("scenes") or {}
            self._scenes_loaded = True
            for idx in range(4):
                if str(idx) in self._scenes:
                    self.scene_btns[idx].config(text=self._scenes[str(idx)].get("name", f"Scene {idx+1}"))
            # align the band selector with the restored frequency (the freq is
            # the source of truth; the band dropdown must not lie)
            band_from_freq = self._band_for_freq(self.freq_hz)
            if band_from_freq:
                self._loading = True
                try:
                    self.band_var.set(band_from_freq)
                finally:
                    self._loading = False
                self._band_stack_current = band_from_freq
            self._sub_refresh_ui()
        except (KeyError, ValueError, tk.TclError):
            pass

    def _bind_slider_wheel(self):
        """Bind mouse wheel to every ttk.Scale: wheel up = increase, down =
        decrease. Default step is 2% of range (Shift = fine 0.5%); sliders can
        pass an explicit step/fine step and an on_change callback.

        IMPORTANT: setting a ttk.Scale's variable does NOT fire its -command,
        so a wheel move would only move the knob and never reach the radio.
        The handler therefore invokes on_change explicitly."""
        def wheel(scale, var, lo, hi, on_change=None, step=None, fine=None):
            def handler(e):
                if scale.instate(["disabled"]):
                    return "break"          # disabled (mode-dependent) = inert
                rng = hi - lo
                if (e.state & 0x0001) and fine is not None:
                    st = fine
                elif step is not None:
                    st = step
                else:
                    st = rng * (0.005 if (e.state & 0x0001) else 0.02)
                d = st if getattr(e, "delta", 120) > 0 else -st
                newv = min(hi, max(lo, var.get() + d))
                if newv == var.get():
                    return "break"
                var.set(newv)
                if on_change is not None:
                    on_change(newv)
                return "break"
            scale.bind("<MouseWheel>", handler)
            scale.bind("<Button-4>", handler)
            scale.bind("<Button-5>", handler)
        for scale, var, lo, hi, cb, step, fine in (
                (self.vol_scale if hasattr(self, "vol_scale") else None,
                 self.vol_var, 0, 100, None, None, None),

                (self.agc_gain_scale, self.agc_gain_var, -20, 120,
                 self._agc_gain_changed, 1.0, 1.0),
                (getattr(self, "filtw_scale", None), self.filtw_var, 10, 20000,
                 self._filtw_changed, 100.0, 10.0),      # fine tuning in Hz
                (getattr(self, "sub_filtw_scale", None), self.sub_filtw_var, 10, 20000,
                 self._sub_filtw_changed, 100.0, 10.0),  # SubVFOA, same steps
                (getattr(self, "sub_agc_gain_scale", None), self.sub_agc_gain_var,
                 -20, 120, self._sub_agc_gain_changed, 1.0, 1.0),
                (self.yzero_scale if hasattr(self, "yzero_scale") else None,
                 self.yzero_var, -40, 40, None, None, None),
                (self.yscale_scale if hasattr(self, "yscale_scale") else None,
                 self.yscale_var, 20, 90, None, None, None),
                (self.zoom_scale if hasattr(self, "zoom_scale") else None,
                 self.zoom_var, 0, 100, None, None, None),
                (self.wf_scale if hasattr(self, "wf_scale") else None,
                 self.wf_gain_var, 0, 100, None, None, None)):
            if scale is not None:
                wheel(scale, var, lo, hi, cb, step, fine)
        # TX DSP sliders: wheel = 1 dB, and the value is sent immediately
        for key, d in getattr(self, "txdsp", {}).items():
            def mk(k):
                def handler(e):
                    if d["scale"].instate(["disabled"]):
                        return "break"
                    self._txdsp_wheel(k, getattr(e, "delta", 120))
                    return "break"
                return handler
            h = mk(key)
            d["scale"].bind("<MouseWheel>", h)
            d["scale"].bind("<Button-4>", h)
            d["scale"].bind("<Button-5>", h)

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
    # ---------------- build UI ----------------
    # Grouping approved by the user: one titled section per owner.
    #   1 GENERAL   connection, audio routing, link state
    #   2 DISPLAY   panafall and its adjust sliders, shared by both receivers
    #   3 VFO A     RX1, reception
    #   4 SubVFOA   SubRX1, same presentation as VFO A, its own DSP chain
    #   5 TX        transmission only
    #   6 LOG       status
    def _section(self, parent, title, expand=False, side=None):
        f = ttk.LabelFrame(parent, text=title, style="Sec.TLabelframe")
        f.pack(fill="both" if expand else "x", expand=expand, side=side,
               padx=8, pady=(5, 1), anchor="n")
        return f

    def _row(self, parent, top=3):
        r = ttk.Frame(parent)
        r.pack(fill="x", padx=6, pady=(top, 0))
        return r

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
        s.configure("Sec.TLabelframe", background=C["panel"], borderwidth=1,
                    relief="solid")
        s.configure("Sec.TLabelframe.Label", background=C["panel"],
                    foreground="#3a4254", font=("Segoe UI", 9, "bold"))
        self.option_add("*TCombobox*Listbox.background", "#ffffff")
        self.option_add("*TCombobox*Listbox.foreground", "#111111")
        self.option_add("*TCombobox*Listbox.selectBackground", "#cce4ff")
        self.option_add("*TCombobox*Listbox.selectForeground", "#000000")
        s.map("TButton", background=[("active", "#c8cfda")])

        # ========================= 1 GENERAL =========================
        # Connection and audio routing: the only controls that act on the
        # program rather than on one receiver or on the transmitter.
        self.sec_general = self._section(self, "1 · GENERAL — CONNECTION")
        g1 = self._row(self.sec_general)
        ttk.Label(g1, text="Receiver:").pack(side="left")
        self.rx_var = tk.StringVar(value="50001 (full TCI)")
        ttk.Combobox(g1, textvariable=self.rx_var, width=15, state="readonly",
                     values=[f"{p} (full TCI)" if p == 50001 else f"{p} (RX{p - 50000})"
                             for p in range(PORT_MIN, PORT_MAX + 1)]
                     ).pack(side="left", padx=(4, 8))
        self.conn_btn = ttk.Button(g1, text="Connect", width=11, command=self.toggle_conn)
        self.conn_btn.pack(side="left")
        ttk.Label(g1, text="Audio:", padding=(20, 0, 2, 0)).pack(side="left")
        self.audiosel_var = tk.StringVar(value="Main")
        ttk.Combobox(g1, textvariable=self.audiosel_var, width=6, state="readonly",
                     values=["Main", "Sub", "Both"]).pack(side="left")
        self.audiosel_var.trace_add("write", self._audiosel_changed)
        ttk.Label(g1, text="A<->B mix:", padding=(16, 0, 2, 0)).pack(side="left")
        self.bal_var = tk.DoubleVar(value=1.0)
        ttk.Scale(g1, from_=0.0, to=1.0, variable=self.bal_var, length=120,
                  command=self._bal_changed).pack(side="left", padx=2)
        self.state_lbl = tk.Label(g1, text="● disconnected", bg=C["panel"],
                                  fg=C["dim"], font=("Segoe UI", 9))
        self.state_lbl.pack(side="right", padx=(0, 6))

        # the sound devices and the output level belong to the program, not to a
        # receiver or to the transmitter
        g2 = self._row(self.sec_general)
        ttk.Label(g2, text="Volume:").pack(side="left", padx=(2, 0))
        self.vol_var = tk.DoubleVar(value=70)
        self.vol_scale = ttk.Scale(g2, from_=0, to=100, variable=self.vol_var, length=150,
                                   command=self._vol_changed)
        self.vol_scale.pack(side="left", padx=4)
        self.volume = 0.7 * 1.2
        self._out_devs = list_output_devices()
        self._in_devs = list_input_devices()
        ttk.Label(g2, text="Speaker:", padding=(14, 0, 2, 0)).pack(side="left")
        self.out_dev_var = tk.StringVar(value="(system default)")
        out_names = ["(system default)"] + [n for _, n, _ in self._out_devs]
        ttk.Combobox(g2, textvariable=self.out_dev_var, width=24, state="readonly",
                     values=out_names).pack(side="left", padx=2)
        self.out_dev_var.trace_add("write", lambda *_: self._reopen_output())
        ttk.Label(g2, text="Mic device:", padding=(14, 0, 2, 0)).pack(side="left")
        self.in_dev_var = tk.StringVar(value="(system default)")
        in_names = ["(system default)"] + [n for _, n, _ in self._in_devs]
        ttk.Combobox(g2, textvariable=self.in_dev_var, width=28, state="readonly",
                     values=in_names).pack(side="left", padx=2)
        self.in_dev_var.trace_add("write", lambda *_: self._reopen_input())
        # the client does not scale the transmit audio itself: the microphone gain
        # is applied by Thetis, so one control has one meaning
        self.mic_gain = 1.0

        # ========================= 2 DISPLAY =========================
        # The panafall draws both receivers, so it belongs to neither of them.
        self.sec_display = self._section(self, "2 · DISPLAY — SPECTRUM AND WATERFALL",
                                        expand=True)
        self.pan = PanFall(self.sec_display)
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
        dz = self._row(self.sec_display, top=4)
        ttk.Label(dz, text="Y zero:").pack(side="left")
        self.yzero_var = tk.DoubleVar(value=0)
        self.yzero_scale = ttk.Scale(dz, from_=-40, to=40, variable=self.yzero_var,
                                     length=150, command=self._yzero_changed)
        self.yzero_scale.pack(side="left", padx=4)
        ttk.Label(dz, text="Y scale:", padding=(16, 0, 0, 0)).pack(side="left")
        self.yscale_var = tk.DoubleVar(value=42)
        self.yscale_scale = ttk.Scale(dz, from_=20, to=90, variable=self.yscale_var,
                                      length=150, command=self._yscale_changed)
        self.yscale_scale.pack(side="left", padx=4)
        ttk.Label(dz, text="Zoom:", padding=(16, 0, 0, 0)).pack(side="left")
        self.zoom_var = tk.DoubleVar(value=0)
        self.zoom_scale = ttk.Scale(dz, from_=0, to=100, variable=self.zoom_var,
                                    length=150, command=self._zoom_changed)
        self.zoom_scale.pack(side="left", padx=4)
        self.zoom_lbl = ttk.Label(dz, text="96 kHz")
        self.zoom_lbl.pack(side="left", padx=6)
        ttk.Label(dz, text="WF intensity:", padding=(16, 0, 0, 0)).pack(side="left")
        self.wf_gain_var = tk.DoubleVar(value=50)
        self.wf_scale = ttk.Scale(dz, from_=0, to=100, variable=self.wf_gain_var,
                                  length=150, command=self._wf_gain_changed)
        self.wf_scale.pack(side="left", padx=4)
        self._row(self.sec_display, top=2)          # breathing space only

        # ---------------- two columns: receivers left, transmit right -------
        mid = ttk.Frame(self)
        mid.pack(fill="x", padx=0, pady=0)
        left = ttk.Frame(mid)
        left.pack(side="left", fill="both", expand=True, anchor="n")
        right = ttk.Frame(mid)
        right.pack(side="left", fill="both", anchor="n")

        # ========================= 3 VFO A =========================
        self.sec_vfoa = self._section(left, "3 · VFO A — RX1 (reception)")
        a1 = self._row(self.sec_vfoa)
        ttk.Label(a1, text="VFO A", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(2, 6))
        self.freq_lbl = tk.Label(a1, text="A 14.074.000", bg=C["panel"], fg=C["tune"],
                                 font=("Consolas", 24, "bold"))
        self.freq_lbl.pack(side="left", padx=(2, 12))
        # Thetis-style per-digit tuning: hover a digit and scroll to change that
        # place value (1 Hz .. 10 MHz). No step buttons.
        self._freq_place = 1000
        self._freq_digit_x = []
        self._freq_font = tkfont.Font(font=self.freq_lbl.cget("font"))
        self._bind_digit_wheel(self.freq_lbl, "A")
        self.ctun_var = tk.BooleanVar(value=False)
        self.ctun_btn = ttk.Checkbutton(a1, text="CTUN", variable=self.ctun_var,
                                        command=self._ctun_toggled)
        self.ctun_btn.pack(side="left", padx=(8, 4))
        self.dds_lbl = tk.Label(a1, text="DDS 7.100.000", bg=C["panel"], fg="#7a4a9a",
                                font=("Consolas", 12, "bold"))
        self.dds_lbl.pack(side="left", padx=(12, 4))
        ttk.Label(a1, text="Direct kHz:", padding=(14, 0, 2, 0)).pack(side="left")
        self.tune_entry = ttk.Entry(a1, width=10)
        self.tune_entry.pack(side="left")
        self.tune_entry.bind("<Return>", lambda e: self._tune_direct())
        ttk.Button(a1, text="Go", width=4, command=self._tune_direct).pack(side="left", padx=4)

        a2 = self._row(self.sec_vfoa)
        ttk.Label(a2, text="Band:").pack(side="left")
        self.band_var = tk.StringVar(value="20m")
        ttk.Combobox(a2, textvariable=self.band_var, width=5, state="readonly",
                     values=[b[0] for b in BANDS]).pack(side="left", padx=(3, 0))
        self.band_var.trace_add("write", self._band_changed)
        ttk.Label(a2, text="Mode:", padding=(14, 0, 2, 0)).pack(side="left")
        self.mode_var = tk.StringVar(value="USB")
        ttk.Combobox(a2, textvariable=self.mode_var, width=5, state="readonly",
                     values=MODES).pack(side="left")
        self.mode_var.trace_add("write", self._mode_changed)
        ttk.Label(a2, text="Filter:", padding=(12, 0, 2, 0)).pack(side="left")
        ttk.Label(a2, text="lo", padding=(2, 0, 2, 0)).pack(side="left")
        self.filt_low_entry = ttk.Entry(a2, width=7)
        self.filt_low_entry.pack(side="left", padx=1)
        self.filt_low_entry.bind("<Return>", self._filter_entries_applied)
        self.filt_low_entry.bind("<FocusOut>", self._filter_entries_applied)
        ttk.Label(a2, text="hi", padding=(6, 0, 2, 0)).pack(side="left")
        self.filt_high_entry = ttk.Entry(a2, width=7)
        self.filt_high_entry.pack(side="left", padx=1)
        self.filt_high_entry.bind("<Return>", self._filter_entries_applied)
        self.filt_high_entry.bind("<FocusOut>", self._filter_entries_applied)
        self.filtw_var = tk.DoubleVar(value=2900)
        self.filtw_scale = ttk.Scale(a2, from_=10, to=20000, variable=self.filtw_var,
                                     length=120, command=self._filtw_changed)
        self.filtw_scale.pack(side="left", padx=(6, 2))
        self.filtw_lbl = tk.Label(a2, text="2.9k", bg=C["panel"], fg=C["fg"],
                                  font=("Consolas", 9, "bold"), width=6)
        self.filtw_lbl.pack(side="left")
        self._filt_updating = False
        ttk.Label(a2, text="AGC:", padding=(14, 0, 2, 0)).pack(side="left")
        self.agc_var = tk.StringVar(value="Med")
        self.agc_box = ttk.Combobox(a2, textvariable=self.agc_var, width=8,
                                    state="readonly", values=AGC_UI_NAMES)
        self.agc_box.pack(side="left")
        self.agc_var.trace_add("write", self._agc_changed)
        ttk.Label(a2, text="Gain:", padding=(8, 0, 2, 0)).pack(side="left")
        self.agc_gain_lbl = tk.Label(a2, text="40", bg=C["panel"], fg=C["fg"],
                                     font=("Consolas", 9, "bold"), width=4)
        self.agc_gain_lbl.pack(side="left", padx=(0, 2))
        self.agc_gain_var = tk.DoubleVar(value=40)
        self.agc_gain_scale = ttk.Scale(a2, from_=-20, to=120, variable=self.agc_gain_var,
                                        length=100, command=self._agc_gain_changed)
        self.agc_gain_scale.pack(side="left", padx=2)
        self.agc_gain_scale.state(["disabled"])

        self.secA_row3 = self._row(self.sec_vfoa)
        # one S-meter per DSP channel: this bar shows VFO A's own signal reading,
        # taken from Thetis (WDSP meters the channel), and the microphone level
        # while transmitting, exactly as Thetis's own meter does
        self.sm = Smeter(self.secA_row3, label="VFO A")
        self.sm.pack(side="left", padx=(2, 0))

        # ======================= 4 SubVFOA =======================
        # Same presentation as VFO A and its OWN DSP chain: its own mode, filter
        # and AGC. Constraint: it runs inside VFO A's slice, one DDC.
        self.sec_sub = self._section(left, "4 · SubVFOA — SubRX1 (reception)")
        b1 = self._row(self.sec_sub)
        ttk.Label(b1, text="SubVFOA", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(2, 6))
        self.vfo_lbl = tk.Label(b1, text="7.076.000", bg=C["panel"], fg="#0055aa",
                                font=("Consolas", 24, "bold"))
        self.vfo_lbl.pack(side="left", padx=(2, 12))
        self._sub_digit_x = []
        self._bind_digit_wheel(self.vfo_lbl, "sub")
        self.sub_btn = ttk.Button(b1, text="SUB off", width=8, command=self._sub_toggle)
        self.sub_btn.pack(side="left", padx=(8, 4))
        ttk.Label(b1, text="Direct kHz:", padding=(14, 0, 2, 0)).pack(side="left")
        self.sub_tune_entry = ttk.Entry(b1, width=10)
        self.sub_tune_entry.pack(side="left")
        self.sub_tune_entry.bind("<Return>", lambda e: self._sub_tune_direct())
        ttk.Button(b1, text="Go", width=4, command=self._sub_tune_direct).pack(side="left", padx=4)

        b2 = self._row(self.sec_sub)
        ttk.Label(b2, text="Mode:").pack(side="left")
        self.submode_var = tk.StringVar(value="USB")
        ttk.Combobox(b2, textvariable=self.submode_var, width=5, state="readonly",
                     values=MODES).pack(side="left", padx=(3, 0))
        self.submode_var.trace_add("write", self._submode_changed)
        ttk.Label(b2, text="Filter:", padding=(12, 0, 2, 0)).pack(side="left")
        ttk.Label(b2, text="lo", padding=(2, 0, 2, 0)).pack(side="left")
        self.sub_filt_low_entry = ttk.Entry(b2, width=7)
        self.sub_filt_low_entry.pack(side="left", padx=1)
        self.sub_filt_low_entry.bind("<Return>", self._sub_filter_entries_applied)
        self.sub_filt_low_entry.bind("<FocusOut>", self._sub_filter_entries_applied)
        ttk.Label(b2, text="hi", padding=(6, 0, 2, 0)).pack(side="left")
        self.sub_filt_high_entry = ttk.Entry(b2, width=7)
        self.sub_filt_high_entry.pack(side="left", padx=1)
        self.sub_filt_high_entry.bind("<Return>", self._sub_filter_entries_applied)
        self.sub_filt_high_entry.bind("<FocusOut>", self._sub_filter_entries_applied)
        self.sub_filtw_var = tk.DoubleVar(value=2650)
        self.sub_filtw_scale = ttk.Scale(b2, from_=10, to=20000, variable=self.sub_filtw_var,
                                         length=120, command=self._sub_filtw_changed)
        self.sub_filtw_scale.pack(side="left", padx=(6, 2))
        self.sub_filtw_lbl = tk.Label(b2, text="2.6k", bg=C["panel"], fg=C["fg"],
                                      font=("Consolas", 9, "bold"), width=6)
        self.sub_filtw_lbl.pack(side="left")
        self._sub_filt_updating = False
        ttk.Label(b2, text="AGC:", padding=(14, 0, 2, 0)).pack(side="left")
        self.sub_agc_var = tk.StringVar(value="Med")
        self.sub_agc_box = ttk.Combobox(b2, textvariable=self.sub_agc_var, width=8,
                                        state="readonly", values=AGC_UI_NAMES)
        self.sub_agc_box.pack(side="left")
        self.sub_agc_var.trace_add("write", self._sub_agc_changed)
        ttk.Label(b2, text="Gain:", padding=(8, 0, 2, 0)).pack(side="left")
        self.sub_agc_gain_lbl = tk.Label(b2, text="40", bg=C["panel"], fg=C["fg"],
                                         font=("Consolas", 9, "bold"), width=4)
        self.sub_agc_gain_lbl.pack(side="left", padx=(0, 2))
        self.sub_agc_gain_var = tk.DoubleVar(value=40)
        self.sub_agc_gain_scale = ttk.Scale(b2, from_=-20, to=120,
                                            variable=self.sub_agc_gain_var, length=100,
                                            command=self._sub_agc_gain_changed)
        self.sub_agc_gain_scale.pack(side="left", padx=2)
        self.sub_agc_gain_scale.state(["disabled"])

        b3 = self._row(self.sec_sub)
        # the sub has its own DSP channel and therefore its own signal reading
        self.sub_sm = Smeter(b3, label="SubVFOA")
        self.sub_sm.pack(side="left", padx=(2, 0))

        # ========================== 5 TX ==========================
        # Everything that only matters while transmitting.
        self.sec_tx = self._section(right, "5 · TX — TRANSMISSION")
        t1 = self._row(self.sec_tx)
        self.ptt_btn = tk.Button(t1, text="PTT", bg="#f4d7d4", fg=C["fg"], width=8,
                                 font=("Segoe UI", 10, "bold"))
        # Thetis parity: MOX is a TOGGLE (click on, click off), one trigger only.
        self.ptt_btn.config(command=self.ptt_toggle)
        self.ptt_btn.pack(side="left", padx=(2, 0))
        self.tune_btn = tk.Button(t1, text="TUNE", bg="#f7e6c8", fg=C["fg"], width=8,
                                  font=("Segoe UI", 10, "bold"), command=self.tune_toggle)
        self.tune_btn.pack(side="left", padx=(8, 0))
        self.tx_lbl = tk.Label(t1, text="RX", bg=C["panel"], fg=C["dim"],
                               font=("Segoe UI", 11, "bold"))
        self.tx_lbl.pack(side="left", padx=12)
        self._space_down = False
        self.bind("<KeyPress-space>", self._space_press)
        self.bind("<KeyRelease-space>", self._space_release)

        t2 = self._row(self.sec_tx)
        ttk.Label(t2, text="TX tail (ms):").pack(side="left", padx=(2, 2))
        self.txtail_var = tk.IntVar(value=350)
        self.txtail_entry = ttk.Entry(t2, width=5)
        self.txtail_entry.insert(0, "350")
        self.txtail_entry.pack(side="left")
        self.txtail_entry.bind("<Return>", self._txtail_entry)
        self.txtail_entry.bind("<FocusOut>", self._txtail_entry)
        ttk.Label(t2, text="Tune drive (%):", padding=(16, 0, 2, 0)).pack(side="left")
        self.tunedrive_entry = ttk.Entry(t2, width=5)
        self.tunedrive_entry.insert(0, str(self.tune_drive_pct))
        self.tunedrive_entry.pack(side="left")
        self.tunedrive_entry.bind("<Return>", self._tunedrive_entry)
        self.tunedrive_entry.bind("<FocusOut>", self._tunedrive_entry)

        self.txdsp = {}
        for key, label, lo, hi, unit in (
                ("mic",  "MIC",  MIC_MIN, MIC_MAX, "dB"),
                ("comp", "COMP", 0, 20, "dB"),
                ("vox",  "VOX",  -80, 0, "dB")):
            r = self._row(self.sec_tx)
            tk.Label(r, text=label, bg=C["panel"], fg=C["fg"], width=5, anchor="w",
                     font=("Segoe UI", 9, "bold")).pack(side="left", padx=(2, 2))
            var = tk.DoubleVar(value=lo)
            sc = ttk.Scale(r, from_=lo, to=hi, variable=var, length=170,
                           command=lambda v, k=key: self._txdsp_drag(k, v))
            sc.pack(side="left")
            sc.bind("<ButtonRelease-1>", lambda e, k=key: self._txdsp_send(k))
            lbl = tk.Label(r, text="off", bg=C["panel"], fg=C["fg"],
                           font=("Consolas", 9, "bold"), width=7, anchor="w")
            lbl.pack(side="left", padx=(4, 0))
            self.txdsp[key] = {"var": var, "scale": sc, "lbl": lbl,
                               "lo": lo, "hi": hi, "unit": unit}
        tdxp = self._row(self.sec_tx)
        # DXP is a TOGGLE BUTTON in Thetis (the gate on or off), so it is one here
        # too; its threshold stays whatever the console holds.
        tk.Label(tdxp, text="DXP", bg=C["panel"], fg=C["fg"], width=5, anchor="w",
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(2, 2))
        self.dexp_btn = tk.Button(tdxp, text="off", width=5,
                                  font=("Segoe UI", 9, "bold"), bg="#e6e8ee",
                                  relief="raised", command=self._dexp_toggle)
        self.dexp_btn.pack(side="left")
        self.dexp_on = False

        # SPLIT is a TRANSMIT decision - it sends the transmitter to the SubVFOA
        # frequency - so it lives here, not with the sub receiver's own controls.
        tsp = self._row(self.sec_tx)
        tk.Label(tsp, text="SPLIT", bg=C["panel"], fg=C["fg"], width=5, anchor="w",
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(2, 2))
        self.split_btn = ttk.Button(tsp, text="off", width=5, command=self._split_toggle)
        self.split_btn.pack(side="left")
        self.tx_src_lbl = tk.Label(tsp, text="TX on VFO A", bg=C["panel"],
                                   fg=C["dim"], font=("Segoe UI", 9))
        self.tx_src_lbl.pack(side="left", padx=(10, 0))

        # ========================= 6 LOG =========================
        self.sec_log = self._section(self, "6 · LOG")
        self.log = tk.Text(self.sec_log, height=5, bg="#ffffff", fg="#333333",
                           borderwidth=1, font=("Consolas", 8))
        self.log.pack(fill="x", padx=6, pady=(2, 6))
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
        """Obsolete: the transmit level is the console microphone gain, driven by
        the TX row's MIC slider. Kept for compatibility with old settings."""
        pass

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

    TX_PREBUFFER_BLOCKS = 2      # silent start until this much mic audio is queued
    TX_MAX_QUEUE_BLOCKS = 8      # never let mic audio age more than this

    def _tx_queue_samples(self):
        """Samples of mic audio waiting. Call with mic_lock held."""
        if not self.tx_audio_q:
            return 0
        return sum(len(b) for b in self.tx_audio_q) - self.tx_pos

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
            # The device may open at its own rate (WASAPI mixer rate): then the
            # requested OUTPUT count needs mic_rate/rate times more INPUT samples.
            # Sending 44.1k-sourced samples labelled 48k makes the rig warble.
            mic_rate = getattr(self, "mic_rate", None)
            need_rs = bool(mic_rate) and abs(mic_rate - float(rate)) > 1.0
            want = (max(1, int(round(vals_needed * mic_rate / float(rate))))
                    if need_rs else vals_needed)
            # start with a short silence until the microphone has buffered a
            # couple of blocks: answering immediately from a half-empty queue
            # makes the stream stutter for the first fraction of a second, and a
            # capture device (especially MME) delivers in bursts.
            with self.mic_lock:
                avail = self._tx_queue_samples()
                while avail > self.TX_MAX_QUEUE_BLOCKS * want and self.tx_audio_q:
                    # bound the latency instead of letting audio age in the queue
                    dropped = self.tx_audio_q.popleft()
                    avail -= len(dropped)
                    self.tx_pos = 0
                if not self._tx_prefilled:
                    if avail >= self.TX_PREBUFFER_BLOCKS * want:
                        self._tx_prefilled = True
                    else:
                        vals = np.zeros(vals_needed, dtype=np.float32)
                        frame = self.build_tx_audio_frame(vals, rate, chans)
                        if self.client and self.client.loop:
                            self.client.send_binary(frame)
                        continue
            # gather mic samples (locked: the PortAudio thread appends)
            mono = bytearray()
            got = 0
            with self.mic_lock:
                while got < want and self.tx_audio_q:
                    blk = self.tx_audio_q[0]
                    take = min(len(blk) - self.tx_pos, want - got)
                    mono += blk[self.tx_pos:self.tx_pos + take].tobytes()
                    got += take
                    self.tx_pos += take
                    if self.tx_pos >= len(blk):
                        self.tx_audio_q.popleft()
                        self.tx_pos = 0
            vals = np.frombuffer(bytes(mono), dtype=np.float32)
            if len(vals) < want:
                # The microphone has not produced this block yet. NEVER answer a
                # chrono with a short frame: the server paces one block per
                # request, so a short frame is an audible gap (the reported
                # 'accrochages'). Pad instead - repeat the last sample when we
                # have data (no click), silence during the initial prefill.
                pad = want - len(vals)
                self.tx_underruns += 1
                fill = (np.full(pad, float(vals[-1]), dtype=np.float32)
                        if len(vals) else np.zeros(pad, dtype=np.float32))
                vals = np.concatenate([vals, fill])
            if need_rs:
                vals = resample(vals, vals_needed)   # exactly the requested count
            vals = np.clip(vals * self.mic_gain, -1.0, 1.0)
            frame = self.build_tx_audio_frame(vals, rate, chans)
            if self.client and self.client.loop:
                self.client.send_binary(frame)
            self._tx_audio_sent += 1

    # ---------------- connect ----------------
    RECONNECT_DELAY_MS = 3000
    RECONNECT_MAX_TRIES = 10

    def _schedule_reconnect(self):
        """After an unexpected drop, retry by itself: the session can vanish
        while the user is not looking, and PTT/Tune then do nothing at all.
        Bounded so a dead server is not hammered; a manual Disconnect cancels."""
        if getattr(self, "_manual_disconnect", False):
            return
        n = getattr(self, "_reconnect_tries", 0) + 1
        self._reconnect_tries = n
        if n > self.RECONNECT_MAX_TRIES:
            self.logprint("reconnect gave up after "
                          f"{self.RECONNECT_MAX_TRIES} attempts - click Connect")
            return
        self.logprint(f"reconnect attempt {n}/{self.RECONNECT_MAX_TRIES} in "
                      f"{self.RECONNECT_DELAY_MS // 1000}s")
        self.after(self.RECONNECT_DELAY_MS, self._do_reconnect)

    def _do_reconnect(self):
        if self.connected or getattr(self, "_manual_disconnect", False):
            return
        if self.client is not None:
            try:
                self.client.send("__close__")
            except Exception:
                pass
            self.client = None
        self.toggle_conn()

    def toggle_conn(self):
        if self.client:
            self._manual_disconnect = True      # a click, not a drop
            self.client.send("__close__")
            self.client = None
            self._set_state("disconnected")
            return
        self._manual_disconnect = False
        port = int(self.rx_var.get().split()[0])
        self._is_full_tci = (port == 50001)  # Phase -1a: port detect
        self.text_q = queue.Queue()
        self._iq_q = queue.Queue(maxsize=8)
        self.chrono_reqs = collections.deque()
        self.tx_audio_q = collections.deque(maxlen=64)
        self.mic_blocks = 0
        self.tx_underruns = 0
        self._tx_audio_sent = 0
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
            self._reconnect_tries = 0
            self.conn_btn.config(text="Disconnect")
            self.state_lbl.config(text="● connected", fg=C["green"])
            self.send("iq_samplerate:96000;")
            self.send("iq_start:0;")
            self.send("audio_start:0;")
            self.send("rx_sensors_enable:true,250;")
            # TX sensors carry Thetis's own microphone reading in dBm, which the
            # S-meter shows while transmitting instead of a local guess
            self.send("tx_sensors_enable:true,250;")
            self.send(f"vfo:0,0,{self.freq_hz};")
            self.send(f"modulation:0,{self.mode};")
            lo, hi = self.pan.filt
            self._send_filter_band(lo, hi)
            # Phase -1a: full TCI (port 50001) uses rx_ctun_ex instead of ctun
            if getattr(self, "_is_full_tci", False):
                self.send(f"rx_ctun_ex:0,{str(self.ctun_var.get()).lower()};")
                self._txdsp_query()   # mic gain / COMP / DXP / VOX from the console
                self.send("tune_drive:0;")   # console transmit power during Tune
                # the SubVFOA's own DSP state, so both apps start in step
                self.send("sub_mode:0;")
                self.send("sub_filter:0;")
                self.send("sub_agc_mode:0;")
                self.send("sub_agc_gain:0;")
                self._vox_mic_keep()
            else:
                self.send(f"ctun:0,{str(self.ctun_var.get()).lower()};")
            # Branch H1: restore subrx state on connect; default B = A + 2 kHz
            if not self.sub_hz:
                self.sub_hz = self.freq_hz + 2000
            if getattr(self, "_is_full_tci", False):
                # 50001: rx_channel_enable for sub-channel, vfoasub for freq
                if self.sub_enabled:
                    self.send(f"rx_channel_enable:0,1,true;")
                    self.send(f"vfoasub:0,{self.sub_hz};")
            else:
                self.send("subrx_state:0;")
                if self.sub_enabled:
                    self.send("subrx:0,true;")
                    self.send(f"vfo:1,0,{self.sub_hz};")
                    self.send(f"sub_mode:0,{self.sub_mode};")
                    self.send(f"sub_filter:0,{self.sub_filt[0]},{self.sub_filt[1]};")
            if self.split:
                self.send("split_enable:0,true;")
            # route audio per the saved main/sub/both selection - NOT the raw
            # balance slider (which only matters in 'both' mode)
            self._apply_audio_selection(force=True)
            if getattr(self, "_is_full_tci", False):
                # 50001: MiniTCI is the AGC master. Push saved state to Thetis.
                # agc_gain is the unified gain path (server routes it through the
                # console AGC-T control, which applies the right parameter for
                # the current mode: fixed gain in Fixed, max gain in auto modes).
                self.send(f"agc_mode:0,{self._agc_mode_to_tci(self.agc_var.get())};")
                self.send(f"agc_gain:0,{int(self.agc_gain_var.get())};")
            elif self.agc_var.get() == "OFF":
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
            if self.ptt or getattr(self, "tuning", False):
                # the socket is gone: nothing can be sent any more, so drop every
                # local TX state instead of showing a key that no longer exists
                self.logprint("session lost while transmitting - clearing TX state")
                self.ptt = False
                self.tuning = False
                self.tune_active = False
                self._key_requested = False
                self._vox_keyed = False
                self._key_false_since = None
                self._tx_visuals()
            self.conn_btn.config(text="Connect")
            self.state_lbl.config(text="● disconnected", fg=C["dim"])
            if was:
                self.logprint("connection lost - reconnecting automatically")
                self._schedule_reconnect()
            else:
                self.logprint("disconnected")

    # ---------------- poll queue ----------------
    def _poll_inner(self):
        # TX audio FIRST, before the display/FFT work: the server paces one audio
        # block per chrono request, so answering late starves the TX chain. Tune
        # tolerated the delay because it synthesises its tone on demand; the mic
        # path reads a queue and is the one that stutters when serviced late.
        self.service_tx_audio()
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

        self._check_key_watchdog()
        # keep the microphone in step with VOX: an armed detector must always have
        # an open input, however the stream was lost (release, device error)
        self._vox_mic_keep()
        self._vox_tick()
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
            # Phase -1a: rx_ctun_ex is the 50001 full-TCI equivalent of ctun
            if k == "rx_ctun_ex" and v:
                p = str(v).split(",")
                try:
                    st = p[1].strip().lower() == "true" if len(p) > 1 else False
                    self.ctun_var.set(st)
                except Exception:
                    pass
                continue
            if k == "rx_channel_enable" and v:
                # 50001: rx_channel_enable:0,1,<bool> — sub-channel on/off
                p = str(v).split(",")
                if len(p) >= 3 and p[1] == "1":
                    st = p[2].strip().lower() == "true"
                    if st != self.sub_enabled:
                        self.sub_enabled = st
                        self.logprint(f"Sub {'on' if st else 'off'} (50001)")
                        self._sub_refresh_ui()
                continue
            if k == "vfoasub" and v:
                # 50001: vfoasub:0,<freqHz> — sub-channel frequency echo
                try:
                    p = str(v).split(",")
                    if len(p) >= 2:
                        self.sub_hz = int(p[1])
                        self._sub_refresh_ui()
                except ValueError:
                    pass
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
                            # Branch H1: vfo:1,0,<hz> = VFO B echo (headless only —
                            # on 50001 vfo:1 is VFOBFreq/RX2, not the sub-frequency)
                            if len(p) >= 3 and p[0] == "1" and not getattr(self, "_is_full_tci", False):
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
                            self.pan.sub_filt = self.sub_filt
                            self._sub_filter_entries_set(self.sub_filt)
                            self._sub_sync_filtw_slider(self.sub_filt)
                    except ValueError:
                        pass
                continue
            if k == "sub_agc_mode" and v:
                # echo format: sub_agc_mode:<trx>,<token>;
                name = self._agc_mode_from_tci(str(v).split(",")[-1].strip().lower())
                self._sub_agc_busy = True
                try:
                    self.sub_agc_var.set(name)
                finally:
                    self._sub_agc_busy = False
                continue
            if k == "sub_agc_gain" and v:
                # echo format: sub_agc_gain:<trx>,<dB>; (sub rx only)
                p = str(v).split(",")
                if len(p) >= 2:
                    try:
                        gain = int(float(p[1]))
                    except ValueError:
                        continue
                    self._sub_agc_gain_busy = True
                    try:
                        self.sub_agc_gain_var.set(gain)
                    finally:
                        self._sub_agc_gain_busy = False
                    self._update_sub_agc_gain_label()
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
                        rx_i, chan_i, hz = int(p[0]), int(p[1]), float(p[2])
                    except ValueError:
                        rx_i = chan_i = -1
                        hz = 0.0
                    # ONLY receiver 0 / channel 0 is VFO A. The 50001 server also
                    # broadcasts vfo:0,1,<hz> for VFOBFreq, and when RX2 is off
                    # VFO B IS the sub-channel - consuming it here dragged VFO A
                    # (and its panadapter marker) onto the sub frequency.
                    if rx_i == 0 and chan_i == 0:
                        self.freq_hz = int(hz)
                        self._fmt_freq()
                        self.pan.vfo_hz = hz
                        # Branch H1: data_center_hz belongs to the DDC (dds echo),
                        # NOT to A - under CTUN A floats inside the DDC and must
                        # not drag the data placement with it.
            elif k == "dds" and v:
                # dds:<rx>,<hz> - CHANNEL-ADDRESSED. Only rx 0 is this client's
                # display centre. With RX2 disabled Thetis still tracks
                # CentreRX2Frequency, and setting VFOBFreq (the sub-channel when
                # RX2 is off) drives it: that fires dds:1,<sub>, which a
                # last-field parse put at the centre of the panadapter.
                p = str(v).split(",")
                try:
                    rx_i = int(p[0])
                    dds_hz = float(p[-1])
                except (ValueError, IndexError):
                    rx_i, dds_hz = -1, 0.0
                if rx_i == 0:
                    try:
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
                    self._tx_visual_state = mox_on
                    self._tx_visuals(mox_on)
                    if mox_on:
                        self.logprint("MOX active (Thetis) - monitor muted")
                    else:
                        self._tx_mute_until = time.time() + self.tx_tail_s
                        self.logprint("MOX released - monitor resumes")
            elif k == "trx" and v:
                # trx:<rx>,<bool>[,tci] - the server's MOX/TUN broadcast. Only
                # rx 0 matters here. Both TX buttons follow, so a Tune or MOX
                # started in Thetis lights MiniTCI too (and vice versa).
                p = str(v).split(",")
                try:
                    if int(p[0]) != 0:
                        raise ValueError
                except (ValueError, IndexError):
                    return
                tx = p[1].lower() == "true" if len(p) > 1 else False
                if tx:
                    self._key_false_since = None
                elif getattr(self, "_key_requested", False):
                    # server says RX while we think we are transmitting: give it
                    # a moment (the echo can trail our own key by a frame), then
                    # clear the local key so nothing stays stuck
                    if self._key_false_since is None:
                        self._key_false_since = time.time()
                self.ptt = tx
                self._tx_visuals()
            elif k == "tune" and v:
                # tune:<rx>,<bool> - Thetis's TUN state. Keeps the two controls
                # independent: a tune carrier lights TUNE, never PTT.
                p = str(v).split(",")
                try:
                    if int(p[0]) != 0:
                        raise ValueError
                    tune_on = p[1].lower() == "true"
                except (ValueError, IndexError):
                    return
                if tune_on != getattr(self, "tune_active", False):
                    self.tune_active = tune_on
                    if tune_on:
                        self.logprint("Thetis TUN active - tune carrier")
                    self._tx_visuals()
            elif k in ("tune_drive", "drive") and v:
                # tune_drive:<rx>,<pct> = the console's TUNE power, drive:<rx>,<pct>
                # = its DRIVE power. Which one governs Tune depends on the
                # console's "tune power origin" setting, and with the drive-slider
                # origin the frames arrive as `drive:`; the Tune level shown here
                # follows whichever the console reports, so it always shows the
                # power a tune will actually use.
                p = str(v).split(",")
                try:
                    if int(p[0]) == 0:
                        self._apply_tune_drive(int(float(p[1])))
                except (ValueError, IndexError):
                    pass
            elif k in ("mic_gain", "tx_comp", "tx_dexp", "vox") and v:
                # Thetis TX microphone/processor state (console -> client)
                self._txdsp_echo({"mic_gain": "mic", "tx_comp": "comp",
                                  "tx_dexp": "dexp", "vox": "vox"}[k], v)
            elif k == "rx_sensors" and v:
                # rx_sensors:<rx>,<dBm>; - the receiver's main channel reading
                try:
                    p = v.split(",")
                    if int(p[0]) == 0:
                        self.smeter = float(p[-1])
                        if 0 not in self.rx_meter_levels:
                            self.rx_meter_levels[0] = self.smeter
                except (ValueError, IndexError):
                    pass
            elif k == "tx_sensors" and v:
                # tx_sensors:<rx>,<mic dBm>,<rms W>,<peak W>,<swr>;
                try:
                    self.tx_mic_dbm = float(str(v).split(",")[1])
                except (ValueError, IndexError):
                    pass
            elif k == "rx_channel_sensors" and v:
                # rx_channel_sensors:<rx>,<channel>,<dBm>[,<avg>,<peak bin>];
                # CHANNEL-ADDRESSED: channel 0 is VFO A, channel 1 the SubVFOA.
                # Each DSP channel has its own meter, which is what gives the sub
                # an S-meter of its own rather than a copy of VFO A's.
                try:
                    p = str(v).split(",")
                    if int(p[0]) == 0 and len(p) >= 3:
                        chan = int(p[1])
                        if chan in (0, 1):
                            self.rx_meter_levels[chan] = float(p[2])
                            if chan == 0:
                                self.smeter = float(p[2])
                except (ValueError, IndexError):
                    pass
            elif k == "rx_filter_band" and v:
                # rx_filter_band:<rx>,<lo>,<hi> - CHANNEL-ADDRESSED; only rx 0
                # is this client's VFO A passband (RX2's filter must not
                # overwrite it).
                p = v.split(",")
                if len(p) >= 3:
                    try:
                        if int(p[0]) == 0:
                            self.pan.filt = (float(p[1]), float(p[2]))
                            self._filter_entries_set(self.pan.filt)
                            self._sync_filtw_slider(self.pan.filt)
                    except ValueError:
                        pass
            elif k == "iq_samplerate" and v:
                try:
                    self.pan.rate = float(int(v))
                except ValueError:
                    pass
            # ---- H1: AGC echo handlers update the UI from the server state ----
            elif k == "agc_mode" and v:
                # server reply: agc_mode:<rx>,<token> (Thetis GUI or our own echo)
                try:
                    token = str(v).split(",")[-1].strip().lower()
                    name = self._agc_mode_from_tci(token)
                    if name:
                        self._agc_busy = True
                        try:
                            self.agc_var.set(name)
                        finally:
                            self._agc_busy = False
                except Exception:
                    pass
            elif k == "agc_gain" and v:
                # Thetis RF slider moved (or our echo) -> keep slider+label in sync
                try:
                    gain = int(float(str(v).split(",")[-1]))
                    self._agc_gain_busy = True
                    try:
                        self.agc_gain_var.set(gain)
                        self._update_agc_gain_label()
                    finally:
                        self._agc_gain_busy = False
                except Exception:
                    pass
            elif k == "modulation" and v:
                # Thetis GUI mode change (or our echo): modulation:<rx>,<TOKEN>.
                # Only rx 0 is this client's VFO A. Thetis sends FM (not NFM).
                try:
                    p = str(v).split(",")
                    if int(p[0]) == 0:
                        tok = p[1].strip().upper()
                        if tok in MODES and tok != self.mode_var.get():
                            # adopt the mode AND the filter Thetis just applied.
                            # Thetis's DRM/SPEC filters are set in SetRX1Mode and
                            # NEVER fire a FilterChangedHandlers broadcast, so the
                            # mode echo is the only sync point - compute the same
                            # filter locally and update the display WITHOUT sending
                            # anything (the server has already applied it).
                            self.mode = tok
                            filt = _thetis_filter(tok, 4)   # mode default
                            if filt is None:
                                self.pan.filt = (-48000, 48000)  # SPEC: full span
                                self._filter_entries_set((-48000, 48000))
                            else:
                                self.pan.filt = filt
                                self._filter_entries_set(filt)
                                self._sync_filtw_slider(filt)
                            try:
                                self.filtw_scale.state(
                                    ["disabled"] if tok in ("DRM", "SPEC", "FM")
                                    else ["!disabled"])
                            except tk.TclError:
                                pass
                            self._mode_busy = True
                            self._submode_busy = True
                            try:
                                self.mode_var.set(tok)
                                if self._is_full_tci:
                                    self.sub_mode = tok
                                    self.submode_var.set(tok)
                                    self.sub_filt = self.pan.filt
                            finally:
                                self._mode_busy = False
                                self._submode_busy = False
                            self._sub_refresh_ui()
                except Exception:
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

    def _bind_digit_wheel(self, lbl, which):
        """Thetis parity, identical for VFO A and SubVFOA: the digit under the
        pointer selects the place value the wheel changes (1 Hz .. 10 MHz)."""
        setattr(self, "_sub_font" if which == "sub" else "_freq_font",
                tkfont.Font(font=lbl.cget("font")))
        attr = "_sub_digit_x" if which == "sub" else "_freq_digit_x"
        lbl.bind("<MouseWheel>", lambda e: self._readout_wheel(e, which))
        lbl.bind("<Button-4>", lambda e: self._readout_wheel(e, which))
        lbl.bind("<Button-5>", lambda e: self._readout_wheel(e, which))
        lbl.bind("<Motion>", lambda e: self._readout_hover(e, which))
        lbl.bind("<Leave>", lambda e: getattr(self, attr).clear())

    def _readout_hover(self, e, which):
        place = self._readout_place(e.x, which)
        if which == "sub":
            self._sub_place = place
        else:
            self._freq_place = place

    def _readout_place(self, x, which):
        """Place value (1 Hz .. 10 MHz) of the digit under x on a readout."""
        if which == "sub":
            lbl, font = self.vfo_lbl, self._sub_font
        else:
            lbl, font = self.freq_lbl, self._freq_font
        txt = lbl.cget("text")
        # digits right to left are 1 Hz, 10 Hz, 100 Hz, 1 kHz, ...
        place = 1
        spans = []
        for i in range(len(txt) - 1, -1, -1):
            if not txt[i].isdigit():
                continue
            try:
                x0 = self._freq_font.measure(txt[:i])
                x1 = x0 + self._freq_font.measure(txt[i])
            except tk.TclError:
                return 1000
            spans.append((x0, x1, place))
            place *= 10
        for x0, x1, p in spans:
            if x0 <= x < x1:
                return p
        # between digits (on a dot) or outside: use the nearest digit's place
        if spans:
            return min(spans, key=lambda sp: abs((sp[0] + sp[1]) / 2 - x))[2]
        return 1000

    def _readout_wheel(self, e, which):
        """Wheel over a readout tunes the digit under the pointer."""
        try:
            place = self._readout_place(e.x, which)
        except (tk.TclError, AttributeError):
            place = 1000
        d = place if getattr(e, "delta", 120) > 0 else -place
        if which == "sub":
            self._sub_place = place
            self._sub_tune_to(max(0, int(self.sub_hz) + d))
        else:
            self._freq_place = place
            self.tune_to(self.freq_hz + d)

    # VFO A entry points, kept for the tests and any caller that names them
    def _freq_place_at(self, x):
        return self._readout_place(x, "A")

    def _freq_wheel(self, e):
        self._readout_wheel(e, "A")

    def rx_meter(self, channel):
        """Thetis's own signal reading for a DSP channel, in dBm, or None.

        0 = VFO A, 1 = the SubVFOA. Fed by the server's rx_channel_sensors
        frames; before the first frame arrives the bar shows idle rather than a
        guess, so a meter that has no measurement never looks like a signal.
        """
        dbm = getattr(self, "rx_meter_levels", {}).get(channel)
        # -400 is WDSP's "no measurement" value for an uninitialised channel
        return None if dbm is None or dbm < -200.0 else dbm

    def _draw_smeter(self):
        """VFO A's bar and the SubVFOA's bar, each from its own DSP channel.

        Thetis meters every channel separately (WDSP RXA_S_PK / RXA_S_AV), so the
        two bars are independent, and both show Thetis's own numbers in dBm with
        Thetis's S-unit labelling.
        """
        if self.ptt:
            # TX: VFO A's bar shows the microphone level driving the transmitter,
            # exactly as Thetis's own meter does during transmit. The bar's scale
            # is dBm, so it uses Thetis's own microphone reading; the local level
            # is still named in the text (it is what the VOX threshold compares
            # against) but does not place the needle, or a dBFS value would be
            # drawn on a dBm scale.
            dbm = getattr(self, "tx_mic_dbm", None)
            self.sm.set_level(dbm)
            if dbm is None:
                local = getattr(self, "mic_level_db", None)
                if local is not None:
                    self.sm.itemconfig(
                        self.sm._txt, text=f"MIC {local:.0f} dBFS (local)",
                        fill=C["fg"])
            self.sub_sm.set_level(None)
            return
        self.sm.set_level(self.rx_meter(0))
        self.sub_sm.set_level(self.rx_meter(1) if self.sub_enabled else None)

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
        self.vfo_lbl.config(text=self._fmt_sub_freq())
        self.sub_btn.config(text="SUB on" if on else "SUB off")
        # SPLIT lives in the TX section: button state and the transmit source
        self.split_btn.config(text="on" if self.split else "off")
        self.tx_src_lbl.config(text="TX on SubVFOA" if self.split else "TX on VFO A",
                               fg=C["red"] if self.split else C["dim"])
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
        if getattr(self, "_is_full_tci", False):
            # 50001: rx_channel_enable controls the sub-channel (chkEnableMultiRX)
            self.send(f"rx_channel_enable:0,1,{str(want).lower()};")
        else:
            self.send(f"subrx:0,{str(want).lower()};")
        if want:
            # the sub channel gets its OWN mode, filter and AGC, on both ports;
            # it only shares VFO A's slice, one DDC
            if not getattr(self, "_is_full_tci", False):
                self.send(f"vfo:1,0,{self.sub_hz};")
            else:
                # vfoasub sets VFOASubFreq (the sub readout in Thetis)
                self.send(f"vfoasub:0,{self.sub_hz};")
            self.send(f"sub_mode:0,{self.sub_mode};")
            self._sub_send_filter()
            self._sub_send_agc()
            # audio routing lives in the General section: keep it in step
            self._apply_audio_selection(force=True)
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

    # ---- SubVFOA: its OWN DSP chain ----------------------------------------
    # The sub runs in VFO A's slice, one DDC, but it has its own mode, filter and
    # AGC, exactly like VFO A. The commands are sub_mode / sub_filter /
    # sub_agc_mode / sub_agc_gain, sent on every port.
    def _submode_changed(self, *_a):
        """Server echoes re-set the var; sending on echo would loop forever."""
        if getattr(self, "_submode_busy", False):
            return
        new_mode = self.submode_var.get()
        if new_mode == getattr(self, "sub_mode", None):
            return                      # same value - nothing to do
        self.sub_mode = new_mode
        # a mode change redefines the passband: the sideband flips with the mode,
        # so apply the mode default and send mode and filter together
        filt = _thetis_filter(new_mode, 4)
        if filt is None:
            filt = (-48000, 48000)          # SPEC: no filter, full span
        self.sub_filt = filt
        self.pan.sub_filt = filt
        self._sub_filter_entries_set(filt)
        self._sub_sync_filtw_slider(filt)
        try:
            self.sub_filtw_scale.state(
                ["disabled"] if new_mode in ("DRM", "SPEC", "FM") else ["!disabled"])
        except tk.TclError:
            pass
        self._sub_send_mode()
        self._sub_send_filter()
        self.logprint(f"SubVFOA mode {new_mode} filter {filt}")

    def _sub_send_mode(self):
        if self._sub_enabled():
            self.send(f"sub_mode:0,{self.sub_mode};")

    def _sub_send_filter(self, filt=None):
        """The sub channel's own passband. DRM/SPEC filters are the server's."""
        if self.submode_var.get() in ("DRM", "SPEC"):
            return
        if not self._sub_enabled():
            return
        lo, hi = filt if filt else self.sub_filt
        self.send(f"sub_filter:0,{int(lo)},{int(hi)};")

    def _sub_send_agc(self):
        if not self._sub_enabled():
            return
        self.send(f"sub_agc_mode:0,{self._agc_mode_to_tci(self.sub_agc_var.get())};")
        self.send(f"sub_agc_gain:0,{int(float(self.sub_agc_gain_var.get()))};")

    def _sub_a_filter(self):
        """SubVFOA passband from its own Low/High boxes."""
        if self.submode_var.get() == "SPEC":
            return None
        try:
            lo = int(float(self.sub_filt_low_entry.get()))
            hi = int(float(self.sub_filt_high_entry.get()))
        except (ValueError, tk.TclError):
            return _thetis_filter(self.submode_var.get(), 4)
        if hi <= lo:
            lo, hi = -3000, 3000
        return (lo, hi)

    def _sub_filter_entries_set(self, filt):
        try:
            lo, hi = filt
            self._sub_filt_updating = True
            self.sub_filt_low_entry.delete(0, "end")
            self.sub_filt_low_entry.insert(0, str(int(lo)))
            self.sub_filt_high_entry.delete(0, "end")
            self.sub_filt_high_entry.insert(0, str(int(hi)))
        except (tk.TclError, ValueError, TypeError):
            pass
        finally:
            self._sub_filt_updating = False

    def _sub_filter_entries_applied(self, *_a):
        """Return or focus-out in the sub's boxes: push the new edges."""
        if self._sub_filt_updating:
            return
        filt = self._sub_a_filter()
        if filt is None:
            return
        self.sub_filt = filt
        self.pan.sub_filt = filt
        self._sub_sync_filtw_slider(filt)
        self._sub_send_filter(filt)
        self.logprint(f"SubVFOA filter {filt[0]}..{filt[1]} Hz")

    def _sub_sync_filtw_slider(self, filt):
        try:
            lo, hi = filt
            bw = hi - lo
            self._sub_filt_updating = True
            try:
                self.sub_filtw_var.set(min(20000, max(10, bw)))
            finally:
                self._sub_filt_updating = False
            self.sub_filtw_lbl.config(
                text=f"{bw/1000:.1f}k" if bw >= 1000 else f"{bw}")
        except (tk.TclError, TypeError, ValueError):
            pass

    def _sub_filtw_changed(self, v):
        """SubVFOA variable bandwidth: the same law as VFO A's slider, applied to
        the sub's own mode."""
        if self._sub_filt_updating:
            return
        mode = self.submode_var.get()
        if mode in ("DRM", "SPEC", "FM"):
            return                      # Thetis disables the slider here
        filt = self._sub_a_filter()
        if filt is None:
            return
        lo, hi = filt
        centre = int((lo + hi) / 2)
        bw = int(float(v))
        if mode in ("USB", "DIGU"):
            new_lo, new_hi = self.LOW_CUT, self.LOW_CUT + bw
        elif mode in ("LSB", "DIGL"):
            new_hi, new_lo = -self.LOW_CUT, -self.LOW_CUT - bw
        elif mode in ("CWL", "CWU"):
            new_lo, new_hi = centre - bw // 2, centre + bw // 2
        else:   # AM, SAM, DSB: symmetric half-bandwidth like Thetis
            new_lo, new_hi = centre - bw, centre + bw
        new_lo, new_hi = self._constrain_filter(new_lo, new_hi, mode)
        self.sub_filt = (new_lo, new_hi)
        self.pan.sub_filt = self.sub_filt
        self._sub_filter_entries_set(self.sub_filt)
        shown = new_hi - new_lo
        self.sub_filtw_lbl.config(
            text=f"{shown/1000:.1f}k" if shown >= 1000 else f"{shown}")
        self._sub_send_filter(self.sub_filt)

    def _sub_agc_changed(self, *_a):
        try:
            self.sub_agc_gain_scale.state(["!disabled"])
        except tk.TclError:
            pass
        self._update_sub_agc_gain_label()
        if getattr(self, "_sub_agc_busy", False):
            return   # echo handler set the var - do not re-send
        self._sub_send_agc()

    def _sub_agc_gain_changed(self, v):
        self._update_sub_agc_gain_label()
        if not self.connected or getattr(self, "_sub_agc_gain_busy", False):
            return
        if self._sub_enabled():
            self.send(f"sub_agc_gain:0,{int(float(v))};")

    def _update_sub_agc_gain_label(self):
        try:
            self.sub_agc_gain_lbl.config(
                text=str(int(float(self.sub_agc_gain_var.get()))))
        except (ValueError, tk.TclError):
            pass

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
            # Phase -1a: full TCI (50001) exposes this as rx_balance; the
            # headless ports use sub_balance
            if getattr(self, "_is_full_tci", False):
                self.send(f"rx_balance:0,{val:.2f};")
            else:
                self.send(f"sub_balance:0,{val:.2f};")
        self._last_audio_sel = sel
        self.logprint(f"audio: {sel} (balance {val:.2f})")

    def _bal_changed(self, v):
        # balance only actively drives placement in 'Both' mode; Main/Sub force
        # hard L/R panning for clean isolation
        if self._sub_enabled() and self.audio_sel == "both":
            if getattr(self, "_is_full_tci", False):
                self.send(f"rx_balance:0,{float(v):.2f};")
            else:
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
            # Phase -1a: 50001 uses vfoasub instead of vfo:1
            if getattr(self, "_is_full_tci", False):
                self.send(f"vfoasub:0,{self.sub_hz};")
            else:
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
        f = hz / 1e6
        for name, (lo, hi) in BAND_RANGES.items():
            if lo <= f < hi:
                return name
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
        # Phase -1a: full TCI (port 50001) uses rx_ctun_ex instead of ctun
        if getattr(self, "_is_full_tci", False):
            self.send(f"rx_ctun_ex:0,{str(self.ctun_var.get()).lower()};")
        else:
            self.send(f"ctun:0,{str(self.ctun_var.get()).lower()};")
        self.logprint(f"CTUN {'on' if self.ctun_var.get() else 'off'}")

    def _clamp_sub_to_ddc(self):
        """Branch H1: VFO B is referenced to the DDC centre, not VFO A. In
        non-CTUN the DDC centre == VFO A, so B sits within +/-48 kHz of A.
        In CTUN the DDC centre stays pinned while A floats, so B can sit at
        the opposite passband edge (up to ~96 kHz from A). The edges are
        FILTER-AWARE: the carrier may reach the DDC edge only where its
        passband does not extend past it (USB sits above, LSB below)."""
        if not self.sub_hz:
            return
        center = self.pan.data_center_hz if self.pan.data_center_hz else self.freq_hz
        off = self.sub_hz - center
        edge = 48000
        fl, fh = self.sub_filt
        upper = edge - max(0, fh)
        lower = -edge + max(0, -fl)
        if off > upper:
            new = int(center + upper)
            self.logprint(f"[clamp] sub={self.sub_hz} center={center:.0f} "
                          f"filt={fl}/{fh} -> {new}")
            self.sub_hz = new
        elif off < lower:
            new = int(center + lower)
            self.logprint(f"[clamp] sub={self.sub_hz} center={center:.0f} "
                          f"filt={fl}/{fh} -> {new}")
            self.sub_hz = new

    def tune_to(self, hz):
        self.freq_hz = int(hz)
        self._fmt_freq()
        # keep VFO B glued to the band/DDC: re-place it relative to the new A
        if self._sub_enabled():
            self._clamp_sub_to_ddc()
            if getattr(self, "_is_full_tci", False):
                self.send(f"vfoasub:0,{self.sub_hz};")
            else:
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
                "filt": [int(self.pan.filt[0]), int(self.pan.filt[1])],
                "b": self.sub_hz, "sub_mode": self.sub_mode,
                "sub_filt": [int(self.sub_filt[0]), int(self.sub_filt[1])],
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
            try:
                fl, fh = int(st["filt"][0]), int(st["filt"][1])
                if fh - fl >= 200:
                    self._filter_entries_set((fl, fh))
                    self.pan.filt = (float(fl), float(fh))
            except (ValueError, TypeError, IndexError):
                pass
        # sub mode BEFORE sub filter: the passband follows the sideband
        self.sub_mode = st.get("sub_mode", "USB")
        self.submode_var.set(self.sub_mode)
        sub_filt = st.get("sub_filt")
        if isinstance(sub_filt, (list, tuple)) and len(sub_filt) == 2:
            self.sub_filt = (int(sub_filt[0]), int(sub_filt[1]))
        elif isinstance(sub_filt, str):
            # legacy scene: a width label. Rebuild the edges for this mode.
            self.sub_filt = _filter_for_mode_width(self.sub_mode, sub_filt) or self.sub_filt
        self._sub_filter_entries_set(self.sub_filt)
        self._sub_sync_filtw_slider(self.sub_filt)
        self.pan.sub_filt = self.sub_filt
        # set the centre synchronously so the restore doesn't clamp B against
        # the previous band's centre
        a = int(st["a"])
        self.pan.center_hz = a
        self.pan.data_center_hz = a
        self.tune_to(a)
        self.sub_hz = int(st.get("b") or 0)
        if self.sub_hz <= 0:
            self.sub_hz = a      # no saved B for this band: default to VFO A
        want_sub = bool(st.get("sub_on"))
        if want_sub and self.connected:
            # Phase -1a: 50001 uses rx_channel_enable + vfoasub; headless uses subrx + vfo:1
            self.sub_enabled = True
            if getattr(self, "_is_full_tci", False):
                self.send("rx_channel_enable:0,1,true;")
                self.send(f"vfoasub:0,{self.sub_hz};")
            else:
                self.send("subrx:0,true;")
                self.send(f"vfo:1,0,{self.sub_hz};")
            self.send(f"sub_mode:0,{self.sub_mode};")
            self._sub_send_filter()
            self._sub_send_agc()
        elif self.sub_enabled:
            if getattr(self, "_is_full_tci", False):
                self.send("rx_channel_enable:0,1,false;")
            else:
                self.send("subrx:0,false;")
            self.sub_enabled = False
        want_split = bool(st.get("split")) and want_sub
        if self.connected:
            self.send(f"split_enable:0,{str(want_split).lower()};")
        self.split = want_split
        self._sub_refresh_ui()

    def _band_changed(self, *_):
        if getattr(self, "_loading", False):
            return                      # settings load in progress - no switch
        name = self.band_var.get()
        prev = getattr(self, "_band_stack_current", None)
        if prev and prev != name:
            self._band_stack_save()
        self._band_stack_current = name
        if self._band_stack_load(name):
            return
        for b in BANDS:
            if b[0] == name:
                new_freq = int(b[1] * 1e6)
                # a band change always re-centres the DDC on A (classic retune,
                # or a CTUN edge jump), so set the client centre synchronously -
                # otherwise B's clamp pins it to the previous band
                self.freq_hz = new_freq
                self._fmt_freq()
                self.pan.vfo_hz = new_freq
                self.pan.center_hz = new_freq
                self.pan.data_center_hz = new_freq
                self.send(f"vfo:0,0,{new_freq};")
                self.mode_var.set(b[2])
                # B follows into the new band, defaulting to VFO A's frequency
                if self.sub_enabled:
                    self._sub_tune_to(new_freq)
                return

    def _a_filter(self):
        """VFO A passband from the Low/High entry boxes. Returns None for
        SPEC (no filter - full DDC span)."""
        if self.mode_var.get() == "SPEC":
            return None
        try:
            lo = int(float(self.filt_low_entry.get()))
            hi = int(float(self.filt_high_entry.get()))
        except (ValueError, tk.TclError):
            return _thetis_filter(self.mode_var.get(), 4)   # mode default
        if hi <= lo:
            lo, hi = -3000, 3000
        return (lo, hi)

    def _mode_changed(self, *_):
        if getattr(self, "_mode_busy", False):
            return   # echo from the server - already in sync
        self.mode = self.mode_var.get()
        filt = _thetis_filter(self.mode_var.get(), 4)   # mode default (F5)
        self._filter_entries_set(filt if filt is not None else (-48000, 48000))
        self.send(f"modulation:0,{self.mode};")
        if filt is not None:
            lo, hi = filt
            self._send_filter_band(lo, hi)
            self.pan.filt = filt
            self._sync_filtw_slider(filt)
        else:
            self.pan.filt = (-48000, 48000)   # SPEC: full DDC span
        # Var slider enabled except in DRM/SPEC/FM (Thetis parity)
        try:
            self.filtw_scale.state(
                ["disabled"] if self.mode in ("DRM", "SPEC", "FM") else ["!disabled"])
        except tk.TclError:
            pass

    def _filter_entries_set(self, filt):
        """Write a (lo, hi) pair into the entry boxes (no send)."""
        try:
            lo, hi = filt
            self._filt_updating = True
            self.filt_low_entry.delete(0, "end")
            self.filt_low_entry.insert(0, str(int(lo)))
            self.filt_high_entry.delete(0, "end")
            self.filt_high_entry.insert(0, str(int(hi)))
        except (tk.TclError, ValueError, TypeError):
            pass
        finally:
            self._filt_updating = False

    def _send_filter_band(self, lo, hi):
        """Send rx_filter_band, except in modes whose DSP filter the server owns
        (DRM/SPEC are set in SetRX1Mode). In DRM the client's window is expressed
        in the dial frame (-5k..+5k) while the server's filter is DDS-relative,
        so sending it would move the demodulation window."""
        if self.mode_var.get() in ("DRM", "SPEC"):
            return
        if self.connected:
            self.send(f"rx_filter_band:0,{int(lo)},{int(hi)};")

    def _filter_entries_applied(self, *_a):
        """User pressed Return / left the box: push the new edges to Thetis."""
        if self._filt_updating:
            return
        filt = self._a_filter()
        if filt is None:
            return
        self.pan.filt = filt
        self._sync_filtw_slider(filt)
        if self.connected:
            lo, hi = filt
            self._send_filter_band(lo, hi)
        self.logprint(f"VFO A filter {filt[0]}..{filt[1]} Hz")

    def _sync_filtw_slider(self, filt):
        """Reflect the current edges in the Var slider + width label."""
        try:
            lo, hi = filt
            bw = hi - lo
            self._filt_updating = True
            try:
                self.filtw_var.set(min(20000, max(10, bw)))
            finally:
                self._filt_updating = False
            self.filtw_lbl.config(
                text=f"{bw/1000:.1f}k" if bw >= 1000 else f"{bw}")
        except (tk.TclError, TypeError, ValueError):
            pass

    LOW_CUT = 150            # Thetis default_low_cut
    MAX_FILTER_SHIFT = 10000  # Thetis _max_filter_shift (ConstrainFilter clamp)

    def _constrain_filter(self, lo, hi, mode=None):
        """Thetis ConstrainFilter parity: clamp edges to the sideband
        convention and to +/-MAX_FILTER_SHIFT. Returns (lo, hi).

        mode defaults to VFO A's; the SubVFOA passes its own mode."""
        if mode is None:
            mode = self.mode_var.get()
        if mode in ("LSB", "DIGL", "CWL"):
            if hi > 0:
                hi = 0
            lo = max(lo, -self.MAX_FILTER_SHIFT)
            hi = min(hi, self.MAX_FILTER_SHIFT)
        elif mode in ("USB", "DIGU", "CWU"):
            if lo < 0:
                lo = 0
            lo = max(lo, -self.MAX_FILTER_SHIFT)
            hi = min(hi, self.MAX_FILTER_SHIFT)
        elif mode in ("AM", "SAM", "DSB", "SPEC"):
            lo = max(lo, -self.MAX_FILTER_SHIFT)
            hi = min(hi, self.MAX_FILTER_SHIFT)
        # FM/DRM: unconstrained (Thetis: no case)
        return (lo, hi)

    def _filtw_changed(self, v):
        """Var1 bandwidth slider - Thetis ptbFilterWidth_Scroll parity.
        New edges per mode family: USB/DIGU lo fixed at LOW_CUT; LSB/DIGL hi
        fixed at -LOW_CUT; CW/DIG centred; AM/SAM/DSB symmetric half-width.
        Clamped by ConstrainFilter. No-op in DRM/SPEC/FM."""
        if self._filt_updating:
            return
        mode = self.mode_var.get()
        if mode in ("DRM", "SPEC", "FM"):
            return                      # Thetis disables the slider here
        filt = self._a_filter()
        if filt is None:
            return
        lo, hi = filt
        centre = int((lo + hi) / 2)
        bw = int(float(v))
        if mode in ("USB", "DIGU"):
            new_lo, new_hi = self.LOW_CUT, self.LOW_CUT + bw
        elif mode in ("LSB", "DIGL"):
            new_hi, new_lo = -self.LOW_CUT, -self.LOW_CUT - bw
        elif mode in ("CWL", "CWU"):
            new_lo, new_hi = centre - bw // 2, centre + bw // 2
        else:   # AM, SAM, DSB: symmetric half-bandwidth like Thetis
            new_lo, new_hi = centre - bw, centre + bw
        new_lo, new_hi = self._constrain_filter(new_lo, new_hi)
        self.pan.filt = (new_lo, new_hi)
        self._filter_entries_set(self.pan.filt)
        shown = new_hi - new_lo
        self.filtw_lbl.config(
            text=f"{shown/1000:.1f}k" if shown >= 1000 else f"{shown}")
        self._send_filter_band(new_lo, new_hi)

    def _apply_mode_filter_default(self):
        """Set the Low/High boxes to the Thetis default for the current mode
        (F5 equivalent) and update the display rect. No send from here."""
        filt = _thetis_filter(self.mode_var.get(), 4)
        self._filter_entries_set(filt)
        self.pan.filt = filt

    def _agc_mode_to_tci(self, name):
        return self.AGC_MODES_TCI.get(name, "normal")

    def _agc_mode_from_tci(self, token):
        return self.AGC_MODES_FROM_TCI.get(token, "Med")

    def _agc_changed(self, *_):
        mode = self.agc_var.get()
        manual = (mode == "Fixed")     # Thetis FIXD
        try:
            self.agc_gain_scale.state(["!disabled"])   # gain applies in ALL modes
        except tk.TclError:
            pass
        self._update_agc_gain_label()
        if not self.connected:
            return
        if getattr(self, "_agc_busy", False):
            return   # echo handler set the var - do not re-send
        # agc_mode alone carries the full mode (server maps off<->FIXD);
        # agc_auto_ex is deliberately NOT sent - on headless it force-maps to
        # MED/FIXD and would clobber e.g. Fast.
        self.send(f"agc_mode:0,{self._agc_mode_to_tci(mode)};")
        # unified gain push: server routes to fixed gain (FIXD) or AGC-T (auto)
        self.send(f"agc_gain:0,{int(self.agc_gain_var.get())};")

    def _agc_auto_changed(self):
        # kept for compatibility (Auto checkbox removed); no-op
        pass

    def _agc_gain_changed(self, v):
        self._update_agc_gain_label()
        if not self.connected:
            return
        if getattr(self, "_agc_gain_busy", False):
            return   # echo handler set the slider - do not re-send
        # one unified gain command on all ports - the server routes it to the
        # correct parameter for the current AGC mode
        self.send(f"agc_gain:0,{int(float(v))};")

    def _update_agc_gain_label(self):
        try:
            self.agc_gain_lbl.config(text=str(int(float(self.agc_gain_var.get()))))
        except (ValueError, tk.TclError):
            pass

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

    def _vfo_at_x(self, x):
        """Return 'B' if the cursor X is over VFO B's passband, 'A' if over
        VFO A's passband, else None (empty waterfall). B wins on overlap."""
        if self._sub_enabled() and self.pan.center_hz and self.pan.sub_hz:
            bx1 = self.pan.f2x(self.pan.sub_hz + self.pan.sub_filt[0])
            bx2 = self.pan.f2x(self.pan.sub_hz + self.pan.sub_filt[1])
            lo, hi = sorted((bx1, bx2))
            if lo - 8 <= x <= hi + 8:    # small grab margin for narrow filters
                return "B"
        if self.pan.center_hz and self.pan.vfo_hz:
            ax1 = self.pan.f2x(self.pan.vfo_hz + self.pan.filt[0])
            ax2 = self.pan.f2x(self.pan.vfo_hz + self.pan.filt[1])
            lo, hi = sorted((ax1, ax2))
            if lo - 8 <= x <= hi + 8:
                return "A"
        return None

    def _pan_click(self, e):
        # Record start and pick which VFO's window was grabbed: VFO B (sub)
        # if over its passband, VFO A if over its passband, else None (empty).
        self._drag_x = e.x
        self._drag_y = e.y
        self._drag_center = self.pan.center_hz
        self._moved = False
        self._freq_pending = None
        self._drag_target = self._vfo_at_x(e.x)   # "A", "B", or None
        if self._drag_target == "B":
            self._drag_vfo = self.pan.sub_hz
            self._drag_filt = self.pan.sub_filt
        elif self._drag_target == "A":
            self._drag_vfo = self.pan.vfo_hz
            self._drag_filt = self.pan.filt
        else:
            self._drag_vfo = None
            self._drag_filt = None

    def _pan_drag(self, e):
        # Quisk OnMotion: dragging slides the grabbed VFO's window; drag speed
        # scales with height above the X axis (near the axis = fine, top = coarse)
        if not self.pan.center_hz or not hasattr(self, "_drag_x"):
            return
        if abs(e.x - self._drag_x) > 2 or abs(e.y - getattr(self, "_drag_y", e.y)) > 2:
            self._moved = True
        if not self._moved:
            return
        if self._drag_target is None:
            return                       # dragged empty: counts as drag, no slide
        speed = max(10.0, PAN_H - e.y) / float(PAN_H + 1)
        dx_hz = speed * (e.x - self._drag_x) / CANVAS_W * self.pan.span
        self._drag_x = e.x   # Quisk accumulates per-motion deltas
        new_f = self._drag_vfo + dx_hz
        self._drag_vfo = new_f
        self._freq_pending = new_f
        if self._drag_target == "B":
            # live-clamp to the filter-aware DDC edges so the blue line never
            # leaves the passband while dragging (snaps back only at the edge)
            center = self.pan.data_center_hz if self.pan.data_center_hz else self.freq_hz
            fl, fh = self.pan.sub_filt
            upper = center + (48000 - max(0, fh))
            lower = center + (-48000 + max(0, -fl))
            new_f = max(lower, min(upper, new_f))
            self.sub_hz = int(new_f)
            self._sub_refresh_ui()
        else:
            # live-follow the frequency display (command still sent on release)
            self.pan.vfo_hz = new_f
            self.freq_hz = int(new_f)
            self._fmt_freq()

    def _pan_release(self, e):
        moved = getattr(self, "_moved", False)
        if self.pan.center_hz and not moved:
            # LEFT click tunes VFO A to the clicked frequency, anywhere on the
            # waterfall (including empty space)
            f = self.pan.x2f(e.x)
            if self.mode in ("CWU", "CWL"):
                f = self._cw_snap(f)
            f = self._round_tune(f)          # Quisk OnLeftUp: re-round to grid
            self.tune_to(f)
            return
        if moved and getattr(self, "_freq_pending", None):
            target = getattr(self, "_drag_target", None)
            if target is None:
                return                       # dragged empty waterfall: nothing
            f = self._round_tune(self._freq_pending)
            if target == "B" and self._sub_enabled():
                self._sub_tune_to(f)
            else:
                self.tune_to(f)

    def _round_tune(self, f):
        # Quisk OnLeftUp FreqRound: snap tune offset to the wheel step grid
        wm = 50
        return int(round(f / wm)) * wm

    def _cw_snap(self, f_click, filt=None):
        # Quisk CW peak snap: search +/- filter width for a peak significantly
        # above the local average, then quadratic-interpolate the peak position
        col = getattr(self.pan, "_col", None)
        if col is None or not self.pan.center_hz:
            return f_click
        fl, fh = filt if filt else self.pan.filt
        x = int(self.pan.f2x(f_click))
        cw_hz = max(200.0, (fh - fl))
        half = max(2, int(cw_hz / self.pan.span * CANVAS_W / 2))
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
        # RIGHT click tunes VFO B to the clicked frequency, anywhere on the
        # waterfall (including empty space)
        if not self.pan.center_hz or not self._sub_enabled():
            return
        f = self.pan.x2f(e.x)
        if self.sub_mode in ("CWU", "CWL"):
            f = self._cw_snap(f, self.pan.sub_filt)
        f = self._round_tune(f)
        self._sub_tune_to(f)

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
        # Quisk OnWheel: fine-tune the VFO under the cursor (VFO B's blue
        # passband, or VFO A's red passband). Empty waterfall = no-op.
        # Shift = 10 Hz, normal = 50 Hz.
        if not self.pan.center_hz:
            return
        target = self._vfo_at_x(e.x)
        if target is None:
            return
        delta = getattr(e, "delta", 120)
        fine = bool(e.state & 0x0001)      # Shift held = fine steps
        step = 10 if fine else 50
        d = step if delta > 0 else -step
        if target == "B" and self._sub_enabled():
            self._sub_tune_to(max(0, self.sub_hz + d))
        else:
            self.tune_to(self.freq_hz + d)

    # ---------------- TX DSP chain (Thetis mirror) ----------------
    TXDSP_CMD = {"mic": "mic_gain", "comp": "tx_comp", "vox": "vox"}
    VOX_HANG_S = 0.7          # how long the carrier holds after speech stops

    # ---- DXP: a toggle, like the console's DEXP button ----------------------
    def _dexp_toggle(self):
        self._dexp_set(not self.dexp_on, send=True)

    def _dexp_set(self, on, send=False):
        self.dexp_on = bool(on)
        try:
            self.dexp_btn.config(text="on" if self.dexp_on else "off",
                                 bg=C["red"] if self.dexp_on else "#e6e8ee",
                                 fg="#ffffff" if self.dexp_on else C["fg"],
                                 relief="sunken" if self.dexp_on else "raised")
        except tk.TclError:
            pass
        if send and self.connected:
            # keep the console's threshold, only switch the gate on or off
            self.send(f"tx_dexp:0,{int(self.dexp_threshold)},"
                      f"{'true' if self.dexp_on else 'false'};")
        if send:
            self.logprint(f"DXP gate {'on' if self.dexp_on else 'off'}")

    # ---- VOX: keys MiniTCI from the microphone level ------------------------
    def _vox_threshold(self):
        """The threshold in dB, or None when the slider sits at its bottom stop.

        Rounded exactly like the value sent to the console, so the local detector
        and Thetis agree on the number (a ttk.Scale can sit between two values).
        """
        d = self.txdsp["vox"]
        v = int(round(float(d["var"].get())))
        return None if v <= d["lo"] else float(v)

    def _vox_tick(self):
        """The console's VOX detector cannot see a client's audio before the
        client transmits, so MiniTCI keys itself: speech above the threshold
        keys, and the carrier holds for VOX_HANG_S after the level drops."""
        thr = self._vox_threshold()
        if thr is None or not self.connected:
            if getattr(self, "_vox_keyed", False):
                self._vox_release()
            return
        if self.tuning:
            return
        lvl = getattr(self, "mic_level_db", -140.0)
        now = time.time()
        if lvl > thr:
            self._vox_above_at = now
            if not self.ptt and not getattr(self, "_vox_keyed", False):
                self._vox_keyed = True
                self.ptt_on()
                self.logprint(f"VOX keyed ({lvl:.0f} dB > {thr:.0f} dB)")
        elif getattr(self, "_vox_keyed", False) and self.ptt:
            if now - getattr(self, "_vox_above_at", 0.0) > self.VOX_HANG_S:
                self.logprint("VOX released")
                self._vox_release()

    def _vox_release(self):
        self._vox_keyed = False
        if self.ptt and not self.tuning:
            self.ptt_off()

    def _vox_mic_keep(self):
        """While VOX is armed the microphone must stay open in receive, or the
        detector has nothing to measure."""
        if self._vox_threshold() is not None and self.connected:
            self._mic_open()
        elif self.mic_stream and not self.ptt:
            try:
                self.mic_stream.stop(); self.mic_stream.close()
            except Exception:
                pass
            self.mic_stream = None
            self.logprint("mic closed (VOX off)")



    def _txdsp_off(self, key):
        """The bottom stop of each control means OFF (the console's own button
        is left off). No separate on/off buttons are needed."""
        d = self.txdsp[key]
        return float(d["var"].get()) <= d["lo"]

    def _txdsp_text(self, key, val=None):
        d = self.txdsp[key]
        if val is None:
            val = float(d["var"].get())
        if val <= d["lo"]:
            return "off"
        if key == "mic":
            return f"{int(round(val))} dB"
        return f"{int(round(val))}"

    def _txdsp_drag(self, key, v):
        """Live readout while the slider moves; nothing is sent until release."""
        if getattr(self, "_txdsp_busy", False):
            return
        try:
            self.txdsp[key]["lbl"].config(text=self._txdsp_text(key, float(v)))
        except tk.TclError:
            pass

    def _txdsp_send(self, key):
        if getattr(self, "_txdsp_busy", False):
            return
        d = self.txdsp[key]
        val = int(round(float(d["var"].get())))
        # snap to the bottom stop so 'off' is exact and repeatable
        if val <= d["lo"] + 1:
            val = d["lo"]
        # park the slider on the value that was sent: a ttk.Scale can sit at
        # -39.6 while the console stores -40, and the two must not drift
        self._txdsp_set(key, val)
        if not self.connected:
            self.logprint("not connected - TX setting not sent")
            return
        self.send(f"{self.TXDSP_CMD[key]}:0,{val};")
        if key == "vox":
            if val <= d["lo"]:
                self.logprint("VOX off")
            else:
                self.logprint(f"VOX armed at {val} dB - MiniTCI keys on speech")
            self._vox_mic_keep()
        elif key == "mic":
            self.logprint(f"microphone gain {'off' if val <= d['lo'] else str(val) + ' dB'}")
        elif key == "comp":
            self.logprint(f"COMP {'off' if val <= 0 else str(val) + ' dB'}")
        elif key == "dexp":
            self.logprint(f"DXP gate {'off' if val <= d['lo'] else str(val) + ' dB'}")

    def _txdsp_set(self, key, val, send=False):
        """Programmatic update (thetis echo / settings restore): no send."""
        d = self.txdsp[key]
        val = max(d["lo"], min(d["hi"], float(val)))
        self._txdsp_busy = True
        try:
            d["var"].set(val)
            d["lbl"].config(text=self._txdsp_text(key, val))
        except tk.TclError:
            pass
        finally:
            self._txdsp_busy = False
        if key == "vox":
            self._vox_mic_keep()       # armed VOX needs the mic open in receive
        if send and self.connected:
            self.send(f"{self.TXDSP_CMD[key]}:0,{int(round(val))};")

    def _txdsp_wheel(self, key, delta):
        d = self.txdsp[key]
        step = 1.0
        v = max(d["lo"], min(d["hi"], float(d["var"].get()) + (step if delta > 0 else -step)))
        self._txdsp_set(key, v)
        self._txdsp_send(key)

    def _txdsp_query(self):
        """Ask Thetis for the current values so the controls start in step."""
        for key in self.txdsp:
            self.send(f"{self.TXDSP_CMD[key]}:0;")
        self.send("tx_dexp:0;")        # drives the DXP toggle button

    def _txdsp_echo(self, key, v):
        """mic_gain|tx_comp|tx_dexp|vox:<rx>,<value>[,<on>]"""
        p = str(v).split(",")
        try:
            if int(p[0]) != 0:
                return
            val = int(p[1])
            on = p[2].strip().lower() == "true" if len(p) > 2 else True
        except (ValueError, IndexError):
            return
        if key == "dexp":
            # the console's gate is a button: mirror its state and threshold
            self.dexp_threshold = val
            self._dexp_set(on)
            return
        if not on:
            val = int(self.txdsp[key]["lo"])             # off = bottom stop
        self._txdsp_set(key, val)

    # ---------------- TX ----------------
    def _tx_visuals(self, keyed=None, tune=None):
        """TUNE and PTT are INDEPENDENT controls (Thetis has separate TUN and MOX
        buttons): a tune carrier lights only TUNE, a mic transmission lights only
        PTT. `keyed` = transmitter state (trx echo/local PTT), `tune` = tune
        carrier state (tune echo/local Tune). While a tune carrier is up the key
        belongs to TUNE, so the PTT button stays dark."""
        if keyed is None:
            keyed = self.ptt
        if tune is None:
            tune = getattr(self, "tune_active", False)
        # Thetis parity: clicking TUN also asserts MOX, so during a tune carrier
        # BOTH controls are active in Thetis - mirror that here.
        ptt_lit = bool(keyed)
        try:
            self.ptt_btn.config(bg=C["red"] if ptt_lit else "#f4d7d4",
                                relief="sunken" if ptt_lit else "raised")
            self.tune_btn.config(bg=C["red"] if tune else "#f7e6c8",
                                 relief="sunken" if tune else "raised")
            if tune:
                self.tx_lbl.config(text="TX \u23fa tune", fg=C["red"])
            elif keyed:
                self.tx_lbl.config(text="TX \u23fa", fg=C["red"])
            else:
                self.tx_lbl.config(text="RX", fg=C["dim"])
        except tk.TclError:
            pass

    def ptt_toggle(self):
        """Click PTT (or press Space) to key, click again to release - Thetis's
        MOX button behaviour. A key that came from Thetis (MOX or a tune
        carrier) is released here too, so the two apps always end up in the
        same state."""
        if self.ptt:
            self.ptt_off()
        else:
            self.ptt_on()

    def _tx_tick(self):
        """Service TX audio on a fast tick while transmitting.

        The normal poll runs every 50 ms and also does the FFT/display work, so a
        slow frame can delay the answer to a chrono by more than one 21 ms audio
        block - the server then starves and the voice stream breaks up. The
        synthesised Tune tone never showed it because it is generated at answer
        time instead of being read from a queue.
        """
        if not self.ptt:
            self._tx_tick_on = False
            return
        try:
            self.service_tx_audio()
        except Exception:
            pass
        self.after(10, self._tx_tick)

    def _start_tx_tick(self):
        if not getattr(self, "_tx_tick_on", False):
            self._tx_tick_on = True
            self.after(10, self._tx_tick)

    def _check_key_watchdog(self):
        """Safety net against a stuck transmitter: if this client believes it is
        keyed but the server has been reporting the key released, drop the local
        state (and the mute) instead of leaving the rig keyed."""
        if not getattr(self, "_key_requested", False):
            self._key_false_since = None
            return
        if self._key_false_since is None:
            return
        if time.time() - self._key_false_since > 2.0:
            self.logprint("key state stale (server says RX) - clearing local PTT")
            self.ptt = False
            self.tuning = False
            self.tune_active = False
            self._key_requested = False
            self._tx_mute_until = time.time() + self.tx_tail_s
            self._key_false_since = None
            self._tx_visuals()

    def ptt_on(self):
        if not self.connected:
            # never look like transmitting while the server has no session
            self.logprint("not connected - PTT ignored (click Connect)")
            self._set_state("disconnected")
            return
        if self.ptt:
            return
        self.ptt = True
        self.send("tx_stream_audio_buffering:100;")
        self.send("audio_stream_sample_type:float32;")
        self.send("audio_stream_channels:1;")
        self.send("audio_stream_samples:1024;")
        self.send("trx:0,true,tci;")     # 'tci' = server takes TX audio from us
        self.tune_active = False         # PTT is the mic path, not the tuner
        self._key_requested = True
        self._key_false_since = None
        with self.mic_lock:
            self.tx_audio_q.clear()
        self.tx_pos = 0
        self.mic_blocks = 0
        self.tx_underruns = 0
        self._tx_audio_sent = 0
        self._tx_prefilled = False
        self._tx_visuals()
        self._start_tx_tick()
        self._mic_open()

    # ---------------- tune ----------------
    def _tunedrive_entry(self, *_):
        """The Tune LEVEL is the console's transmit power during Tune.

        Scaling the client's tone amplitude looks like a level control but is
        not one: the transmit chain's compander and ALC normalise a steady tone,
        so anything above their threshold came out at full power and anything
        below it vanished - the 'either 0% or 100%' behaviour. The tone is now
        sent at a fixed, sane amplitude and the percentage drives the console
        (TCI tune_drive, which follows the console's own 'tune power origin'
        setting: the drive slider or the tune slider)."""
        raw = self.tunedrive_entry.get().strip()
        try:
            pct = int(float(raw))
        except ValueError:
            self._apply_tune_drive(self.tune_drive_pct)
            return
        pct = max(1, min(100, pct))
        self._apply_tune_drive(pct, send=True)

    def _apply_tune_drive(self, pct, send=False):
        """Show and (optionally) push the tune percentage."""
        pct = max(1, min(100, int(pct)))
        self.tune_drive_pct = pct
        self.tune_amp = TUNE_TONE_AMP              # fixed tone level
        try:
            self.tunedrive_entry.delete(0, "end")
            self.tunedrive_entry.insert(0, str(pct))
        except tk.TclError:
            pass
        if send:
            if self.connected:
                self.send(f"tune_drive:0,{pct};")
                self.logprint(f"Tune drive {pct}% (console transmit power)")
            else:
                self.logprint("not connected - tune drive not sent")

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
        # trx claims the TX audio stream ('tci'); tune:<rx>,true drives Thetis's
        # own TUN button so both apps show the SAME source (without it Thetis
        # shows MOX and MiniTCI's PTT appears to fire with the tuner)
        self.send("trx:0,true,tci;")
        self.send("tune:0,true;")
        self.tune_active = True
        self._key_requested = True
        self._key_false_since = None
        self._tx_prefilled = True      # the tone needs no microphone prefill
        self._tx_visuals()
        self._start_tx_tick()
        self.logprint(f"Tune ON: 1500 Hz tone at {self.tune_drive_pct}% console power")

    def tune_stop(self):
        if not self.tuning:
            return
        self.tuning = False
        self.send("tune:0,false;")       # release Thetis's TUN as well
        self.tune_active = False
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
        self._vox_keyed = False
        if getattr(self, "tuning", False):
            # a tune carrier must be dropped on Thetis too, not only the key
            self.tuning = False
            self.send("tune:0,false;")
        self.send("trx:0,false,tci;")
        self.tune_active = False
        self._key_requested = False
        self._key_false_since = None
        self._tx_visuals()
        if self.tuning:
            pass
        elif self._tx_audio_sent:
            # give the user a number to compare: a healthy transmission answers
            # every chrono with real samples (underruns stay near zero)
            self.logprint(f"TX audio: {self._tx_audio_sent} blocks, "
                          f"{self.mic_blocks} mic blocks, "
                          f"{self.tx_underruns} underruns")
        # Do NOT close the microphone unconditionally here. With VOX armed the
        # microphone is the detector's input: closing it on release left
        # mic_level_db frozen at -140 dB, so the FIRST transmission worked and
        # every later one silently never keyed ("unless I move the slider a
        # bit", which re-opened it through _vox_mic_keep). _vox_mic_keep closes
        # the device when VOX is off, which is the case that actually wants it.
        self._vox_mic_keep()

    def _space_press(self, e):
        """Space = TX toggle, same as the button. Key auto-repeat must not flip
        the state repeatedly, so only the first press counts."""
        if not getattr(self, "_space_down", False):
            self._space_down = True
            self.ptt_toggle()

    def _space_release(self, e):
        self._space_down = False

    def _space_dn(self, e):
        self.ptt_on()

    def _space_up(self, e):
        self.ptt_off()

    def _mic_open(self):
        if self.mic_stream:
            return
        if time.time() < getattr(self, "_mic_retry_after", 0.0):
            return                      # a device that just failed: do not spin on it
        try:
            kw = {"samplerate": MIC_RATE, "channels": 1, "dtype": "float32",
                  "blocksize": 1024, "callback": self._mic_cb}
            dev = self._find_dev(self._in_devs, self.in_dev_var.get())
            if dev is not None:
                kw["device"] = dev
            self.mic_stream = sd.InputStream(**kw)
            self.mic_stream.start()
            name = self.in_dev_var.get() if dev is not None else "system default"
            # the device may run at its own rate/size; the TX path must know both
            self.mic_rate = float(getattr(self.mic_stream, "samplerate", MIC_RATE))
            blk = getattr(self.mic_stream, "blocksize", 0)
            self.logprint(f"mic open: {name} {self.mic_rate:.0f} Hz, {blk} frames")
            if abs(self.mic_rate - TX_AUDIO_RATE) > 1.0:
                self.logprint(f"note: mic runs at {self.mic_rate:.0f} Hz, "
                              f"resampling to {TX_AUDIO_RATE} Hz for TX")
        except Exception as e:
            self._mic_retry_after = time.time() + 2.0
            self.logprint(f"mic error: {e}")

    def _reopen_input(self):
        if self.ptt:
            self._mic_open()

    def _mic_cb(self, indata, frames, t, status):
        """PortAudio thread: append only - NEVER touch tx_pos here.

        tx_pos is the read cursor into the head block of tx_audio_q. Resetting it
        on every callback re-sent the not-yet-consumed tail of a partially read
        block, so slices of the microphone signal were repeated each callback
        period - an audible stutter/oscillation that is NOT acoustic feedback.
        Only the consumer advances (and resets) it."""
        if not self.connected:
            return
        # level first: the VOX detector needs it even while receiving
        try:
            rms = float(np.sqrt(np.mean(indata.astype(np.float64) ** 2)))
            self.mic_level_db = 20.0 * np.log10(rms + 1e-10)
        except Exception:
            pass
        if self.ptt:
            with self.mic_lock:
                self.tx_audio_q.append(indata.reshape(-1).copy())
                self.mic_blocks += 1

    # ---------------- log ----------------
    LOG_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                            "MiniTCI", "minitci.log")

    def logprint(self, s):
        """Every line goes to the log widget AND to a file.

        The file matters for diagnosis: a session that drops and reconnects reports
        the reason once (a reader or watchdog error), and by the time anyone looks
        the window is often gone. The file survives the session.
        """
        line = f"[{time.strftime('%H:%M:%S')}] {s}"
        try:
            self.log.insert("end", line + "\n")
            self.log.see("end")
            if float(self.log.index("end-1c").split(".")[0]) > 60:
                self.log.delete("1.0", "20.0")
        except Exception:
            pass
        try:
            os.makedirs(os.path.dirname(self.LOG_PATH), exist_ok=True)
            if os.path.getsize(self.LOG_PATH) > 1_000_000:
                with open(self.LOG_PATH, "r", errors="ignore") as f:
                    tail = f.readlines()[-2000:]
                with open(self.LOG_PATH, "w", errors="ignore") as f:
                    f.writelines(tail)
            with open(self.LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


if __name__ == "__main__":
    MiniTCI().mainloop()