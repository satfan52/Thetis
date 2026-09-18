#!/usr/bin/env python3
"""Run MiniTCI's own client headless for N seconds: connect, pump the Tk loop,
report every drop, reconnect and error line it produces.

  python probe_session_stability.py [seconds] [port]

Used to compare a source run against the frozen exe: the server log line
[TCIServer.handleAudioStart] is emitted once per connect burst, so counting it
over a window shows whether the session is stable or cycling.
"""
import sys
import time

sys.path.insert(0, ".")
import MiniTCI as M

SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 40.0
PORT = sys.argv[2] if len(sys.argv) > 2 else "50001 (full TCI)"

app = M.MiniTCI()
app.withdraw()
app._loading = True
app.vol_var.set(0)
lines = []
orig = app.logprint


def cap(s):
    lines.append((time.time(), s))
    orig(s)


app.logprint = cap
app.rx_var.set(PORT)
app.toggle_conn()
t0 = time.time()
n0 = 0
events = 0
while time.time() - t0 < SECONDS:
    app.update()
    time.sleep(0.01)
    if len(lines) > n0:
        for ts, l in lines[n0:]:
            low = l.lower()
            if any(w in low for w in ("error", "reconnect", "watchdog", "closing",
                                      "disconnect", "gave up")):
                events += 1
                print(f"  [{ts - t0:5.1f}s] {l}")
        n0 = len(lines)
print(f"connected at end: {app.connected} | reconnect events: {events} | "
      f"tries counter: {app._reconnect_tries}")
app.destroy()
