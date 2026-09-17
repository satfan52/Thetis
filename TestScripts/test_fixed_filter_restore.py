#!/usr/bin/env python3
"""Regression test: a stale persisted filter for a FIXED-filter mode (DRM,
SPEC) must not survive a settings load.

A settings file written by an older build carried DRM edges 7000..10000; on
restart MiniTCI restored them and the user saw the wrong DRM window (+10000).
Fixed-filter modes must always come from _thetis_filter(), never the file.

  python test_fixed_filter_restore.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import MiniTCI as M

fails = []


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        fails.append(name)


STALE = {
    "mode": "DRM",
    "filt": [7000, 10000],          # what the old build persisted
    "sub_mode": "DRM",
    "sub_filt": [7000, 10000],
    "host": "127.0.0.1",
    "port": 50001,
}

tmp = tempfile.mkdtemp(prefix="minitci_cfg_")


def write_settings(d):
    os.makedirs(tmp, exist_ok=True)
    p = os.path.join(tmp, "settings.json")
    with open(p, "w") as f:
        json.dump(d, f)
    return p


def main():
    p = write_settings(STALE)
    app = M.MiniTCI()
    try:
        app.withdraw()
        M.MiniTCI.SETTINGS_PATH = p                  # point the loader at our file
        app._loading = True
        app._load_settings()
        app.update()
        check("DRM low forced to 7000", app.filt_low_entry.get().strip(), "7000")
        check("DRM high forced to 17000", app.filt_high_entry.get().strip(), "17000")
        check("DRM pan.filt forced", tuple(int(x) for x in app.pan.filt), (7000, 17000))
        check("DRM slider label = width 10k", app.filtw_lbl.cget("text"), "10.0k")

        # a normal mode keeps the persisted edges (user's own Var setting)
        p2 = write_settings({"mode": "USB", "filt": [150, 4200], "host": "127.0.0.1"})
        M.MiniTCI.SETTINGS_PATH = p2
        app._loading = True
        app._load_settings()
        app.update()
        check("USB keeps persisted edges",
              (app.filt_low_entry.get().strip(), app.filt_high_entry.get().strip()),
              ("150", "4200"))

        # SPEC: full DDC span, never a persisted pair
        p3 = write_settings({"mode": "SPEC", "filt": [7000, 10000], "host": "127.0.0.1"})
        M.MiniTCI.SETTINGS_PATH = p3
        app._loading = True
        app._load_settings()
        app.update()
        check("SPEC forced to full span",
              tuple(int(x) for x in app.pan.filt), (-48000, 48000))
    finally:
        app.destroy()

    print()
    if fails:
        print(f"{len(fails)} FAILED: {fails}")
        return 1
    print("all fixed-filter restore checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())