#!/usr/bin/env python3
"""Reproduce the VOX threshold oddity: drive MiniTCI's own VOX slider through a
range of values against the LIVE server and report what is sent, what the
server echoes back, and what the client's keying threshold becomes.

Read-only in RF terms: nothing here keys the transmitter (the VOX engine is not
ticked and the client is not switched to transmit).

  python probe_vox_values.py [port]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import MiniTCI as M

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 50001
VALUES = [-39, -40, -41, -40, -39, -40]

app = M.MiniTCI()
app.withdraw()
app.logprint = lambda s: None

sent = []
app.send = lambda c: sent.append(c)
app.connected = True
app._is_full_tci = True
app.client = type("C", (), {"loop": False, "send_binary": lambda self, b: None})()

print(f"{'set':>5}  {'sent':<18} {'engine thr':>10}  {'slider':>8}  echo -> slider/thr")
for v in VALUES:
    app._txdsp_set("vox", v)          # as the echo/restore path would
    sent.clear()
    app._txdsp_send("vox")            # as the user's release would
    sent_cmd = sent[-1] if sent else "(nothing)"
    thr = app._vox_threshold()
    # now simulate the server's echo of what it actually holds
    held = int(sent_cmd.split(",")[1].rstrip(";")) if sent else v
    app.tci_text({"vox": f"0,{held},true"})
    t0 = time.time()
    while time.time() - t0 < 0.25:
        app.update()
        time.sleep(0.01)
    print(f"{v:>5}  {sent_cmd:<18} {str(thr):>10}  "
          f"{app.txdsp['vox']['var'].get():>8.1f}  "
          f"{app.txdsp['vox']['var'].get():.0f}/{app._vox_threshold()}")

# the OFF case: below the bottom stop
print()
app._txdsp_set("vox", -80)
sent.clear()
app._txdsp_send("vox")
print("bottom stop ->", sent[-1] if sent else "(nothing)", "| threshold:",
      app._vox_threshold())
app.tci_text({"vox": "0,-40,false"})     # console says VOX is off
t0 = time.time()
while time.time() - t0 < 0.25:
    app.update()
    time.sleep(0.01)
print("after an OFF broadcast -> slider", app.txdsp["vox"]["var"].get(),
      "| threshold", app._vox_threshold())
app.destroy()