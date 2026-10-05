#!/usr/bin/env python3
"""Read-only TCI probe: RMS of the RX1 vs RX2 audio on port 50001, plus the reported VFOs.

Tunes nothing, keys nothing. Prints the peak/RMS per receiver so silence is
distinguishable from a real signal (the Red Pitaya noise floor is far above zero).
"""
import math
import struct
import time

from websockets.sync.client import connect


def read_banner(ws, timeout=1.5):
    d = {}
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            m = ws.recv(timeout=0.3)
        except TimeoutError:
            continue
        if isinstance(m, str):
            for c in m.split(";"):
                if ":" in c:
                    k, v = c.split(":", 1)
                    d.setdefault(k.strip(), []).append(v.strip())
    return d


def measure(ws, seconds):
    """Return per-receiver frame counts and RMS of float32 payloads."""
    out = {}
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            m = ws.recv(timeout=1.0)
        except TimeoutError:
            continue
        if not (isinstance(m, bytes) and len(m) >= 64):
            continue
        ft = struct.unpack("<I", m[24:28])[0]
        if ft != 1:
            continue
        rx = struct.unpack("<I", m[0:4])[0]
        body = m[64:]
        n = len(body) // 4
        if n <= 0:
            continue
        vals = struct.unpack("<%df" % n, body[: 4 * n])
        pk = max(abs(v) for v in vals)
        rms = math.sqrt(sum(v * v for v in vals) / n)
        e = out.setdefault(rx, {"frames": 0, "peak": 0.0, "rms_sum": 0.0})
        e["frames"] += 1
        e["peak"] = max(e["peak"], pk)
        e["rms_sum"] += rms
    for rx, e in out.items():
        e["rms"] = e["rms_sum"] / e["frames"] if e["frames"] else 0.0
    return out


with connect("ws://127.0.0.1:50001/") as ws:
    b = read_banner(ws)
    print("console-reported state from the banner:")
    for k in ("vfo", "rx_enable", "mute", "rx_volume", "modulation", "dds", "trx", "rx_channel_enable"):
        if k in b:
            print("   %-18s %s" % (k, "; ".join(b[k])))
    ws.send("audio_stream_sample_type:float32;")
    ws.send("audio_stream_channels:1;")
    ws.send("audio_stream_samples:4096;")
    ws.send("audio_start:0;")
    ws.send("audio_start:1;")
    time.sleep(0.5)
    res = measure(ws, 6.0)
    print("\naudio measured over 6 s:")
    for rx in sorted(res):
        e = res[rx]
        label = "RX1 (TRX 0)" if rx == 0 else ("RX2 (TRX 1)" if rx == 1 else "receiver %d" % rx)
        print("   %-14s frames=%-4d peak=%.4f  mean RMS=%.5f" % (label, e["frames"], e["peak"], e["rms"]))
    print("\n   reference: a silent stream reads RMS 0.00000; the Red Pitaya noise floor is")
    print("   typically 0.0005 to 0.02 float scale at an open band.")
    ws.send("audio_stop:0;")
    ws.send("audio_stop:1;")
