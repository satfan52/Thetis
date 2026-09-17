#!/usr/bin/env python3
"""Read-only: report how the microphone device really opens (rate, block size,
delivered callback size). No radio, no TX.

  python probe_mic_device.py [substring-of-device-name]
"""
import collections
import sys
import time

import numpy as np
import sounddevice as sd

want = sys.argv[1].lower() if len(sys.argv) > 1 else "microphone"
dev = None
for i, d in enumerate(sd.query_devices()):
    if d["max_input_channels"] > 0 and want in d["name"].lower():
        dev = i
        print(f"device {i}: {d['name']} | host API "
              f"{sd.query_hostapis(d['hostapi'])['name']} | "
              f"in {d['max_input_channels']}ch | default rate {d['default_samplerate']:.0f}")
        break

sizes = collections.Counter()
info = {}


def cb(indata, frames, t, status):
    sizes[frames] += 1
    if status:
        info.setdefault("status", str(status))


try:
    st = sd.InputStream(device=dev, samplerate=48000, channels=1,
                        dtype="float32", blocksize=1024, callback=cb)
    st.start()
    print(f"opened: requested 48000 Hz / blocksize 1024")
    print(f"actual : {st.samplerate:.0f} Hz / blocksize {st.blocksize}")
    time.sleep(1.5)
    st.stop()
    st.close()
    print("callback block sizes seen:", dict(sizes))
    if "status" in info:
        print("stream status flags:", info["status"])
    else:
        print("no stream status flags (clean)")
except Exception as e:
    print("open failed:", type(e).__name__, e)
