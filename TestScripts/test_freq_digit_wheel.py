#!/usr/bin/env python3
"""Regression test: Thetis-style per-digit tuning on the frequency readout.

Hovering a digit and scrolling must change THAT place value (1 Hz .. 10 MHz);
the old +/-10k/1k/100 buttons are gone.  Also covers the DRM default window
(-5000..+5000, dial-relative, no filter command sent to the server).

  python test_freq_digit_wheel.py
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
        sent = []
        app.connected = True
        app.send = lambda c: sent.append(c)

        # ---- DRM default window -----------------------------------------
        app.mode_var.set("DRM")
        app.update()
        check("DRM default low", app.filt_low_entry.get().strip(), "-5000")
        check("DRM default high", app.filt_high_entry.get().strip(), "5000")
        check("DRM window in dial frame", tuple(int(v) for v in app.pan.filt),
              (-5000, 5000))
        check("DRM sends no rx_filter_band",
              [c for c in sent if "rx_filter_band" in c], [])

        # ---- per-digit place mapping -------------------------------------
        app.mode_var.set("USB")
        app.update()
        app.freq_hz = 14_074_000
        app._fmt_freq()
        app.update()
        txt = app.freq_lbl.cget("text")
        check("readout format", txt, "14.074.000")
        check("wheel is bound on the readout",
              bool(app.freq_lbl.bind("<MouseWheel>")), True)
        check("hover tracking is bound",
              bool(app.freq_lbl.bind("<Motion>")), True)
        places = {}
        for i, ch in enumerate(txt):
            if ch.isdigit():
                x = app._freq_font.measure(txt[:i]) + 2
                places[10 ** (len(txt) - i - txt[i:].count(".") - 1)] = \
                    app._freq_place_at(x)
        # every digit maps to its own place value
        check("MHz digit (leftmost)", app._freq_place_at(
            app._freq_font.measure(txt[:0]) + 1), 10_000_000)
        check("1 Hz digit (rightmost)", app._freq_place_at(
            app._freq_font.measure(txt[:-1]) + 2), 1)

        # ---- wheel actually tunes the hovered digit ----------------------
        class Ev:                     # synthetic wheel event over a digit
            def __init__(self, x, delta):
                self.x, self.y, self.delta, self.state = x, 8, delta, 0

        def wheel_at(char_index, delta):
            x = app._freq_font.measure(txt[:char_index]) + 2
            before = app.freq_hz
            sent.clear()
            app._freq_wheel(Ev(x, delta))     # the bound handler itself
            app.update()
            return app.freq_hz - before, sent

        d, cmd = wheel_at(0, 120)          # leftmost digit = 10 MHz
        check("wheel on 10 MHz digit", d, 10_000_000)
        check("wheel tuned and sent", cmd[:1], [f"vfo:0,0,{app.freq_hz};"])

        d, _ = wheel_at(5, 120)            # kHz digit = 1 kHz
        check("wheel on 1 kHz digit", d, 1_000)

        d, _ = wheel_at(9, 120)            # last digit = 1 Hz
        check("wheel on 1 Hz digit", d, 1)

        d, _ = wheel_at(4, -120)           # 10 kHz digit, downwards
        check("wheel down on 10 kHz digit", d, -10_000)

        # carry: 14.074.999 + 1 Hz rolls the kHz group
        app.freq_hz = 14_074_999
        app._fmt_freq()
        app.update()
        txt = app.freq_lbl.cget("text")
        d, _ = wheel_at(9, 120)
        check("carry from 1 Hz into kHz", (d, app.freq_hz), (1, 14_075_000))

        # ---- step buttons are gone ---------------------------------------
        def all_texts(w, acc):
            for c in w.winfo_children():
                try:
                    acc.append(str(c.cget("text")))
                except Exception:
                    pass
                all_texts(c, acc)
            return acc
        texts = all_texts(app, [])
        check("no +10k step button", [t for t in texts if t == "+10k"], [])
        check("no +1k step button", [t for t in texts if t == "+1k"], [])
        check("no +100 step button", [t for t in texts if t == "+100"], [])
    finally:
        app.destroy()

    print()
    if fails:
        print(f"{len(fails)} FAILED: {fails}")
        return 1
    print("all per-digit wheel checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())