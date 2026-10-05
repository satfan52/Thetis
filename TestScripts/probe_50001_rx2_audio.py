#!/usr/bin/env python3
"""Read-only TCI probe: does port 50001 stream RX2 audio (TRX 1)?

Connects, reads the banner, negotiates RX audio and asks for receiver 1, then
for receiver 0, counting binary RX audio frames (frame type 1) per receiver
index. Tunes nothing, keys nothing.
"""
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
                    d[k.strip()] = v.strip()
    return d


def count_rx_audio(ws, seconds):
    counts = {}
    texts = []
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            m = ws.recv(timeout=1.0)
        except TimeoutError:
            continue
        if isinstance(m, bytes) and len(m) >= 64:
            ft = struct.unpack("<I", m[24:28])[0]
            rx = struct.unpack("<I", m[0:4])[0]
            if ft == 1:
                counts[rx] = counts.get(rx, 0) + 1
        elif isinstance(m, str):
            texts.append(m.strip())
    return counts, texts


with connect("ws://127.0.0.1:50001/") as ws:
    b = read_banner(ws)
    print("banner:")
    for k in ("device", "trx_count", "channels_count", "vfo", "rx_enable", "ready", "protocol"):
        if k in b:
            print("   %-16s %s" % (k, b[k]))
    print("   (all keys: %s)" % ", ".join(sorted(b.keys())))

    ws.send("audio_stream_sample_type:float32;")
    ws.send("audio_stream_channels:1;")
    ws.send("audio_stream_samples:4096;")
    time.sleep(0.3)

    print("\n--- requesting RX2 audio:  audio_start:1;")
    ws.send("audio_start:1;")
    c1, t1 = count_rx_audio(ws, 5.0)
    print("    RX audio frames by receiver index: %s" % c1)
    if t1:
        print("    text seen: %s" % t1[-6:])
    ws.send("audio_stop:1;")
    time.sleep(0.3)

    print("\n--- requesting RX1 audio:  audio_start:0;")
    ws.send("audio_start:0;")
    c0, t0_ = count_rx_audio(ws, 5.0)
    print("    RX audio frames by receiver index: %s" % c0)
    if t0_:
        print("    text seen: %s" % t0_[-6:])
    ws.send("audio_stop:0;")

    print("\n--- both: audio_start:0 and 1")
    ws.send("audio_start:0;")
    ws.send("audio_start:1;")
    cb, tb = count_rx_audio(ws, 5.0)
    print("    RX audio frames by receiver index: %s" % cb)
    if tb:
        print("    text seen: %s" % tb[-6:])
