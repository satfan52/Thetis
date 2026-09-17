#!/usr/bin/env python3
"""Headless-port (50002-50008) echo regression test.

Feeds the SHIPPED _handle the EXACT frames the headless server emits (literals
taken from HeadlessTciServer.cs: BroadcastVfo 574/575, dds 1735/1765/1772,
vfo:1,0 sub 1716, rx_filter_band 585/1487/1800/1809, subrx 1718/1831/1836,
sub_mode 1852/1856, sub_filter 1867, sub_balance 1878, subrx_state 1929,
modulation 580/1486/1782, if 1750, trx) and asserts the headless scheme still
works after the port-50001 channel-addressing fixes.

  python test_headless_echoes.py
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
        app._loading = True
        app._is_full_tci = False          # headless port, e.g. 50003

        A = 14_074_000
        SUB = 14_080_000                 # sub inside A's DDC passband

        def reset():
            app.freq_hz = A
            app.pan.vfo_hz = float(A)
            app.pan.center_hz = float(A)
            app.pan.data_center_hz = float(A)
            app.pan.filt = (-2900.0, -100.0)
            app.sub_hz = A
            app.sub_filt = (150, 2850)
            app.sub_mode = "USB"
            app.sub_enabled = False

        # --- VFO A: server sends vfo:0,0 and dds:0 (BroadcastVfo + dds) -----
        reset()
        app._handle({"vfo": "0,0,14080000"})
        check("headless vfo:0,0 sets VFO A", app.freq_hz, 14_080_000)
        check("headless vfo:0,0 sets A marker", app.pan.vfo_hz, 14_080_000.0)

        app._handle({"dds": "0,14080000"})
        check("headless dds:0 sets display centre", app.pan.center_hz, 14_080_000.0)
        check("headless dds:0 sets data centre", app.pan.data_center_hz, 14_080_000.0)

        # BroadcastVfo() sends channel 1 with the SAME frequency - must not
        # disturb anything (it is a redundant echo of VFO A).
        reset()
        app._handle({"vfo": "0,1,14074000"})
        check("headless vfo:0,1 leaves VFO A", app.freq_hz, A)
        check("headless vfo:0,1 leaves A marker", app.pan.vfo_hz, float(A))

        # --- VFO B / sub: server sends vfo:1,0 (line 1716) -------------------
        reset()
        app.sub_enabled = True
        app._handle({"vfo": f"1,0,{SUB}"})
        check("headless vfo:1,0 sets sub frequency", app.sub_hz, SUB)
        check("headless vfo:1,0 leaves VFO A", app.freq_hz, A)
        check("headless vfo:1,0 marks sub enabled", app.sub_enabled, True)

        # --- passband: rx_filter_band:0,lo,hi --------------------------------
        reset()
        app._handle({"rx_filter_band": "0,150,2850"})
        check("headless rx_filter_band:0 sets A passband", app.pan.filt, (150.0, 2850.0))

        # --- sub enable / mode / filter / balance ---------------------------
        reset()
        app._handle({"subrx": "0,true"})
        check("headless subrx:0,true enables sub", app.sub_enabled, True)
        app._handle({"subrx": "0,false"})
        check("headless subrx:0,false disables sub", app.sub_enabled, False)

        reset()
        app.sub_enabled = True
        app._handle({"sub_mode": "0,LSB"})
        check("headless sub_mode echo sets mode", app.sub_mode, "LSB")
        app._handle({"sub_filter": "0,-2850,-150"})
        check("headless sub_filter echo sets filter", app.sub_filt, (-2850, -150))

        # --- subrx_state snapshot (7 fields) --------------------------------
        reset()
        app._handle({"subrx_state": f"0,true,{SUB},CWU,500,2300,0.50"})
        check("headless subrx_state enables sub", app.sub_enabled, True)
        check("headless subrx_state sets sub freq", app.sub_hz, SUB)
        check("headless subrx_state sets sub mode", app.sub_mode, "CWU")
        check("headless subrx_state sets sub filter", app.sub_filt, (500, 2300))
        check("headless subrx_state leaves VFO A", app.freq_hz, A)

        # --- trx / mox / sensors -------------------------------------------
        reset()
        app._handle({"trx": "0,true"})
        check("headless trx:0,true sets PTT", app.ptt, True)
        app._handle({"trx": "0,false"})
        check("headless trx:0,false clears PTT", app.ptt, False)
    finally:
        try:
            app.destroy()
        except Exception:
            pass

    print()
    if fails:
        print(f"FAILED: {len(fails)} -> {fails}")
        return 1
    print("all headless echo checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())