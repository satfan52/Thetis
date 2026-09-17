#!/usr/bin/env python3
"""Drive the REAL MiniTCI app (its own connect + poll loop) headlessly and
report whether the session survives.  No TX - read-only connect.

  python probe_app_connect.py [port] [seconds]
"""
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import MiniTCI as M

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 50001
SECS = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0

errors = []

app = M.MiniTCI()
app.withdraw()


def hook(exc, val, tb):
    errors.append("".join(traceback.format_exception(exc, val, tb)))


app.report_callback_exception = hook

# select the requested receiver in the dropdown, exactly like the user
opts = app.rx_var.cget("values") if hasattr(app.rx_var, "cget") else []
sel = next((o for o in opts if str(o).startswith(str(PORT))), None)
if sel:
    app.rx_var.set(sel)
print("receiver selected:", app.rx_var.get(), flush=True)

logs = []
app.logprint = lambda s: (logs.append(str(s)), print("   LOG:", s, flush=True))

app.toggle_conn()

t0 = time.time()
while time.time() - t0 < SECS:
    try:
        app.update()
    except Exception as e:
        print("update error:", e, flush=True)
    time.sleep(0.02)

print(f"\nconnected={app.connected}  state_label={app.state_lbl.cget('text')!r}")
print("iq queue:", app._iq_q.qsize(), " text queue:", app.text_q.qsize())
print("\nlast log lines:")
for l in logs[-12:]:
    print("   ", l)
if errors:
    print("\nCALLBACK EXCEPTIONS:")
    for e in errors[:3]:
        print(e)
else:
    print("\nno callback exceptions")
app.destroy()