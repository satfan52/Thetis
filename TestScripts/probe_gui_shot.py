#!/usr/bin/env python3
"""Screenshot MiniTCI's own window, so the layout can be checked as the user
sees it. Writes a PNG next to this script and prints its path.

  python probe_gui_shot.py [outfile.png]
"""
import os
import sys
import time

sys.path.insert(0, ".")
import MiniTCI as M

out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "minitci_window.png")

app = M.MiniTCI()
app._loading = True
app.vol_var.set(0)
app.update()
app.deiconify()
app.lift()
t0 = time.time()
while time.time() - t0 < 1.5:
    app.update()
    time.sleep(0.02)

x = app.winfo_rootx()
y = app.winfo_rooty()
w = app.winfo_width()
h = app.winfo_height()
print(f"window at {x},{y} size {w}x{h}")

try:
    from PIL import ImageGrab
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    img.save(out)
    print("saved:", out)
except Exception as e:                     # noqa: BLE001
    print("grab failed:", type(e).__name__, e)
app.destroy()
