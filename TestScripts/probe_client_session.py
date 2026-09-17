#!/usr/bin/env python3
"""Drive MiniTCI's OWN TciClient headlessly against the live TCI port and log
every state transition - reproduces 'connected then immediately disconnected'
without touching the GUI.  Read-only (no PTT).

  python probe_client_session.py [port] [seconds]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import MiniTCI as M

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 50001
SECS = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0
FREQ = 7_097_917

t0 = time.time()
events = []


def stamp():
    return f"{time.time() - t0:6.2f}s"


def on_state(s):
    events.append(f"{stamp()}  STATE  {s}")
    print(events[-1], flush=True)
    if s == "connected":
        for cmd in ("iq_samplerate:96000", "iq_start:0", "audio_start:0",
                    "rx_sensors_enable:true,250", f"vfo:0,0,{FREQ}",
                    "modulation:0,LSB", "rx_filter_band:0,-2800,-100",
                    "rx_ctun_ex:0,false", "rx_channel_enable:0,1,true",
                    "vfoasub:0,7077020", "subrx_state:0", "subrx:0,true",
                    "vfo:1,0,7077020", "sub_mode:0,LSB",
                    "sub_filter:0,-1800,-100", "split_enable:0,true",
                    "rx_balance:0,0.50", "agc_mode:0,normal", "agc_gain:0,81"):
            c.send(cmd)


def on_text(d):
    if "__error__" in d:
        print(f"{stamp()}  ERROR  {d['__error__']}", flush=True)
    elif "__state__" in d:
        on_state(d["__state__"])


c = M.TciClient(PORT)
app = M.MiniTCI()          # not used; only so the module's Tk bits exist
app.withdraw()
c.start(on_text, lambda *a: None, lambda *a: None, on_state, lambda *a: None)

deadline = time.time() + SECS
while time.time() < deadline:
    time.sleep(1.0)
    print(f"{stamp()}  ...", flush=True)

print("\n--- transitions ---")
for e in events:
    print("  " + e)
app.destroy()