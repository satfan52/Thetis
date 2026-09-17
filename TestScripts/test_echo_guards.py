#!/usr/bin/env python3
"""Regression test for channel-addressed TCI echoes on port 50001.

Runs the SHIPPED handler (_handle) in-process against synthetic frames and
asserts that only the frames addressed to receiver 0 / channel 0 may move
VFO A, the display centre or VFO A's passband.

  python test_echo_guards.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import MiniTCI as M

fails = []


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        fails.append(name)


def main():
    app = M.MiniTCI()
    try:
        app.withdraw()
        app._loading = True          # suppress side effects of var writes
        app._is_full_tci = True      # behave as a port-50001 client

        A = 14_074_000
        SUB = 7_074_000              # deliberately a different band

        def reset():
            app.freq_hz = A
            app.pan.vfo_hz = float(A)
            app.pan.center_hz = float(A)
            app.pan.data_center_hz = float(A)
            app.pan.filt = (-2900.0, -100.0)

        # --- dds: only rx 0 may move the display centre ---------------------
        reset()
        app._handle({"dds": f"1,{SUB}"})
        check("dds:1 leaves display centre", app.pan.center_hz, float(A))
        check("dds:1 leaves data centre", app.pan.data_center_hz, float(A))

        app._handle({"dds": "0,14070000"})
        check("dds:0 moves display centre", app.pan.center_hz, 14_070_000.0)
        check("dds:0 moves data centre", app.pan.data_center_hz, 14_070_000.0)

        # --- vfo: only rx 0 / chan 0 is VFO A ------------------------------
        reset()
        app._handle({"vfo": f"0,1,{SUB}"})          # VFOBFreq echo (sub when RX2 off)
        check("vfo:0,1 leaves VFO A", app.freq_hz, A)
        check("vfo:0,1 leaves A marker", app.pan.vfo_hz, float(A))

        app._handle({"vfo": "1,0,7074000"})         # RX2's VFO
        check("vfo:1,0 leaves VFO A", app.freq_hz, A)
        check("vfo:1,0 leaves A marker", app.pan.vfo_hz, float(A))

        app._handle({"vfo": "0,0,14080000"})
        check("vfo:0,0 moves VFO A", app.freq_hz, 14_080_000)
        check("vfo:0,0 moves A marker", app.pan.vfo_hz, 14_080_000.0)

        # --- rx_filter_band: only rx 0 may set VFO A's passband -------------
        reset()
        app._handle({"rx_filter_band": "1,-3000,3000"})
        check("rx_filter_band:1 leaves A passband", app.pan.filt, (-2900.0, -100.0))
        app._handle({"rx_filter_band": "0,-2900,-100"})
        check("rx_filter_band:0 sets A passband", app.pan.filt, (-2900.0, -100.0))

        # --- vfoasub drives the SUB frequency, never VFO A ------------------
        reset()
        app._handle({"vfoasub": f"0,{SUB}"})
        check("vfoasub sets sub frequency", app.sub_hz, SUB)
        check("vfoasub leaves VFO A", app.freq_hz, A)
        check("vfoasub leaves centre", app.pan.center_hz, float(A))
    finally:
        try:
            app.destroy()
        except Exception:
            pass

    print()
    if fails:
        print(f"FAILED: {len(fails)} -> {fails}")
        return 1
    print("all echo-guard checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
