#!/usr/bin/env python3
"""Regression test: mouse wheel on MiniTCI sliders must reach the radio.

Setting a ttk.Scale's variable programmatically does NOT fire its -command,
so a wheel move used to move the knob while sending nothing (AGC gain and
filter bandwidth both frozen).  These checks drive the real bound handler.

  python test_slider_wheel.py
"""
import os
import sys

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
        app._is_full_tci = True

        sent = []
        app.connected = True
        app.send = lambda cmd: sent.append(cmd)

        # --- filter bandwidth slider -------------------------------------
        app.mode_var.set("USB")
        app.mode = "USB"
        app._filter_entries_set((150, 3050))          # 100..3000 shifted? no:
        app.pan.filt = (150.0, 3050.0)                # deliberate 2.9k Var1
        app._sync_filtw_slider(app.pan.filt)
        app._filt_updating = False

        bw0 = app.filtw_var.get()
        sent.clear()
        app.filtw_scale.event_generate("<MouseWheel>", delta=120)
        app.update()
        check("filter wheel sends rx_filter_band", len(sent), 1)
        if sent:
            check("filter wheel command", sent[0], "rx_filter_band:0,150,3150;")
        check("filter wheel grew width by 100 Hz",
              round(app.filtw_var.get() - bw0, 3), 100.0)

        # fine step: Shift+wheel = 10 Hz
        sent.clear()
        bw1 = app.filtw_var.get()
        app.filtw_scale.event_generate("<MouseWheel>", delta=-120, state=0x0001)
        app.update()
        check("filter Shift+wheel is fine (10 Hz)",
              round(bw1 - app.filtw_var.get(), 3), 10.0)

        # disabled in DRM: wheel must be inert
        app.mode_var.set("DRM")
        app.mode = "DRM"
        app.filtw_scale.state(["disabled"])
        sent.clear()
        v_before = app.filtw_var.get()
        app.filtw_scale.event_generate("<MouseWheel>", delta=120)
        app.update()
        check("filter wheel inert in DRM", (len(sent), app.filtw_var.get()),
              (0, v_before))
        app.filtw_scale.state(["!disabled"])

        # --- AGC gain slider ---------------------------------------------
        app.agc_gain_scale.state(["!disabled"])
        app.agc_gain_var.set(40)
        num0 = app.agc_gain_var.get()
        sent.clear()
        app.agc_gain_scale.event_generate("<MouseWheel>", delta=120)
        app.update()
        check("agc wheel sends agc_gain", len(sent), 1)
        if sent:
            check("agc wheel command", sent[0], "agc_gain:0,41;")
        check("agc wheel stepped 1 dB", round(app.agc_gain_var.get() - num0, 3), 1.0)

        sent.clear()
        app.agc_gain_scale.event_generate("<MouseWheel>", delta=-120)
        app.update()
        check("agc wheel down", sent, ["agc_gain:0,40;"])
    finally:
        app.destroy()

    print()
    if fails:
        print(f"{len(fails)} FAILED: {fails}")
        return 1
    print("all slider-wheel checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())