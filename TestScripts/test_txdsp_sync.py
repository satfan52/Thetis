#!/usr/bin/env python3
"""Regression test: the MiniTCI TX row mirrors Thetis' microphone and processor
chain (microphone gain, compander, downward expander, VOX).

Rules under test:
  * four sliders exist, each carrying the console's own unit;
  * each slider's BOTTOM stop means OFF (no on/off buttons), and the off state
    is sent as the bottom value;
  * values are pushed on release only (never during the drag);
  * the console's broadcasts update the sliders without echoing back;
  * connect queries the four values.

  python test_txdsp_sync.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import MiniTCI as M

fails = []


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        fails.append(name)


def pump(app, secs=0.35):
    t0 = time.time()
    while time.time() - t0 < secs:
        app.update()
        time.sleep(0.01)


def main():
    app = M.MiniTCI()
    try:
        app.withdraw()
        app._loading = True
        sent = []
        app.connected = True
        app.send = lambda c: sent.append(c)

        check("four TX sliders", sorted(app.txdsp.keys()),
              ["comp", "dexp", "mic", "vox"])
        check("MIC uses the console range",
              (app.txdsp["mic"]["lo"], app.txdsp["mic"]["hi"]),
              (M.MIC_MIN, M.MIC_MAX))
        check("COMP range", (app.txdsp["comp"]["lo"], app.txdsp["comp"]["hi"]),
              (0, 20))
        check("DXP range", (app.txdsp["dexp"]["lo"], app.txdsp["dexp"]["hi"]),
              (-160, 0))
        check("VOX range", (app.txdsp["vox"]["lo"], app.txdsp["vox"]["hi"]),
              (-80, 0))

        # ---- the bottom stop = off, and that is what is sent --------------
        for key, want_cmd in (("mic", "mic_gain:0,-40;"),
                              ("comp", "tx_comp:0,0;"),
                              ("dexp", "tx_dexp:0,-160;"),
                              ("vox", "vox:0,-80;")):
            sent.clear()
            app._txdsp_set(key, app.txdsp[key]["lo"])
            app._txdsp_send(key)
            check(f"{key.upper()} off sends the bottom value", sent, [want_cmd])
            check(f"{key.upper()} reads 'off'", app.txdsp[key]["lbl"].cget("text"),
                  "off")

        # ---- a real value is sent as the console unit ----------------------
        sent.clear()
        app._txdsp_set("mic", -18)
        app._txdsp_send("mic")
        check("MIC -18 dB is sent", sent, ["mic_gain:0,-18;"])
        check("MIC readout", app.txdsp["mic"]["lbl"].cget("text"), "-18 dB")

        sent.clear()
        app._txdsp_set("comp", 7)
        app._txdsp_send("comp")
        check("COMP 7 dB is sent", sent, ["tx_comp:0,7;"])

        sent.clear()
        app._txdsp_set("dexp", -100)
        app._txdsp_send("dexp")
        check("DXP -100 dB is sent", sent, ["tx_dexp:0,-100;"])

        sent.clear()
        app._txdsp_set("vox", -40)
        app._txdsp_send("vox")
        check("VOX -40 dB is sent", sent, ["vox:0,-40;"])

        # ---- dragging must not send (only the readout moves) --------------
        sent.clear()
        app._txdsp_drag("mic", -30)
        check("drag sends nothing", sent, [])
        check("drag updates the readout", app.txdsp["mic"]["lbl"].cget("text"),
              "-30 dB")

        # ---- console broadcasts drive the sliders -------------------------
        app.tci_text({"mic_gain": "0,-12,true"})
        pump(app)
        check("mic_gain echo sets the slider", app.txdsp["mic"]["var"].get(), -12.0)
        sent.clear()
        pump(app, 0.1)
        check("echo does not talk back", sent, [])

        app.tci_text({"tx_comp": "0,5,true"})
        pump(app)
        check("tx_comp echo sets the slider", app.txdsp["comp"]["var"].get(), 5.0)

        # an OFF broadcast parks the slider at the bottom stop
        app.tci_text({"mic_gain": "0,-40,false"})
        pump(app)
        check("mic OFF broadcast parks MIC at the bottom",
              app.txdsp["mic"]["var"].get(), float(M.MIC_MIN))
        check("MIC shows off", app.txdsp["mic"]["lbl"].cget("text"), "off")

        app.tci_text({"vox": "0,-55,true"})
        pump(app)
        check("vox echo sets the slider", app.txdsp["vox"]["var"].get(), -55.0)

        # another receiver's frames are ignored
        app.tci_text({"tx_comp": "1,9,true"})
        pump(app)
        check("frames for rx 1 ignored", app.txdsp["comp"]["var"].get(), 5.0)

        # ---- query on connect --------------------------------------------
        sent.clear()
        app._txdsp_query()
        check("connect query", sent,
              ["mic_gain:0;", "tx_comp:0;", "tx_dexp:0;", "vox:0;"])

        # ---- wheel steps 1 dB and sends ----------------------------------
        sent.clear()
        app._txdsp_set("comp", 7)
        app._txdsp_wheel("comp", 120)
        check("wheel up = +1 dB and sent",
              (app.txdsp["comp"]["var"].get(), sent), (8.0, ["tx_comp:0,8;"]))
        sent.clear()
        app._txdsp_wheel("comp", -120)
        check("wheel down = -1 dB and sent",
              (app.txdsp["comp"]["var"].get(), sent), (7.0, ["tx_comp:0,7;"]))

        # ---- persistence ---------------------------------------------------
        snap = app._settings_snapshot()
        check("settings carry the TX values", isinstance(snap.get("txdsp"), dict)
              and sorted(snap["txdsp"].keys()), ["comp", "dexp", "mic", "vox"])
    finally:
        app.destroy()

    print()
    if fails:
        print(f"{len(fails)} FAILED: {fails}")
        return 1
    print("all TX-DSP sync checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())