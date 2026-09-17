#!/usr/bin/env python3
"""AUTHORISED key test: 2 s Tune from MiniTCI's own TX path, then release.

Sets the console drive LOW for the test and restores it afterwards. Reports
whether the server's trx echo (MOX) matched, i.e. whether Thetis actually went
into transmit while MiniTCI transmitted.

  python probe_tune_keytest.py [port] [drive_percent]
"""
import asyncio
import os
import re
import sys
import time

import websockets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import MiniTCI as M

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 50001
DRIVE = int(sys.argv[2]) if len(sys.argv) > 2 else 5
KEY_SECS = 2.0

async def read_drive(port):
    """Current console drive in percent (the tune_drive echo)."""
    async with websockets.connect(f"ws://127.0.0.1:{port}", ping_interval=None) as ws:
        await ws.send("protocol:1;")
        await ws.send("start;")
        await asyncio.sleep(0.5)
        try:
            while True:
                await asyncio.wait_for(ws.recv(), timeout=0.3)
        except asyncio.TimeoutError:
            pass
        await ws.send("drive:0;")
        t0 = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - t0 < 3:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if isinstance(msg, bytes):
                continue
            for fr in msg.split(";"):
                f = fr.strip().lower()
                if f.startswith("tune_drive") or f.startswith("drive"):
                    m = re.findall(r"\d+", f)
                    if m:
                        await ws.send("stop;")
                        return int(m[-1])
        return None


ORIG_DRIVE = asyncio.run(read_drive(PORT))
print("original drive:", ORIG_DRIVE, flush=True)

app = M.MiniTCI()
app.withdraw()
app.report_callback_exception = lambda *a: print("  CALLBACK EXC:", a[1])

logs = []
app.logprint = lambda s: (logs.append(str(s)), print("   LOG:", s, flush=True))

app.rx_var.set(str(PORT))       # the port is parsed from the first token
app.toggle_conn()

t0 = time.time()
while time.time() - t0 < 12 and not app.connected:
    app.update()
    time.sleep(0.02)
print("connected:", app.connected)
if not app.connected:
    app.destroy()
    sys.exit("could not connect - aborting before any TX")

# low drive for the test
app.send(f"drive:0,{DRIVE};")
for _ in range(60):
    app.update()
    time.sleep(0.01)
print(f"drive set to {DRIVE}% for the test")

# 5% of full scale tone
app.tunedrive_entry.delete(0, "end")
app.tunedrive_entry.insert(0, "5")
app._tunedrive_entry()
print("tune drive:", app.tune_amp)

timeline = []
ptt_seen = False
try:
    print(f"\n>>> TUNE ON for {KEY_SECS}s", flush=True)
    txframes = [0]
    _orig_send_bin = app.client.send_binary
    def _count_bin(b, _o=_orig_send_bin, _c=txframes):
        _c[0] += 1
        return _o(b)
    app.client.send_binary = _count_bin
    chronos_before = len(app.chrono_reqs)
    app.tune_toggle()
    t1 = time.time()
    while time.time() - t1 < KEY_SECS:
        app.update()
        timeline.append((round(time.time() - t1, 2), app.ptt,
                         getattr(app, "mox_active", None)))
        if app.ptt:
            ptt_seen = True
        time.sleep(0.02)
finally:
    print(">>> TUNE OFF", flush=True)
    try:
        app.tune_stop()
    except Exception as e:
        print("tune_stop error:", e)
        app.send("trx:0,false,tci;")
    t2 = time.time()
    while time.time() - t2 < 1.5:
        app.update()
        time.sleep(0.02)

print("\n--- results ---")
print("MOX observed while tuning:", ptt_seen)
print("TX chrono requests from the server:", len(app.chrono_reqs) - chronos_before)
print("TX_AUDIO frames sent to the server:", txframes[0])
print("ptt at end:", app.ptt, " mox_active:", getattr(app, "mox_active", None))
print("samples:", timeline[:4], "...", timeline[-4:])
restore = ORIG_DRIVE if ORIG_DRIVE is not None else 0
app.send(f"drive:0,{restore};")
for _ in range(40):
    app.update()
    time.sleep(0.01)
print(f"drive restored to {restore}%")
print("\nlog tail:")
for l in logs[-10:]:
    print("   ", l)
app.destroy()