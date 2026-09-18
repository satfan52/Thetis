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

        check("three TX sliders plus the DXP button", sorted(app.txdsp.keys()),
              ["comp", "mic", "vox"])
        check("DXP is a button, not a slider", hasattr(app, "dexp_btn"), True)
        check("MIC uses the console range",
              (app.txdsp["mic"]["lo"], app.txdsp["mic"]["hi"]),
              (M.MIC_MIN, M.MIC_MAX))
        check("COMP range", (app.txdsp["comp"]["lo"], app.txdsp["comp"]["hi"]),
              (0, 20))
        check("VOX range", (app.txdsp["vox"]["lo"], app.txdsp["vox"]["hi"]),
              (-80, 0))

        # ---- the bottom stop = off, and that is what is sent --------------
        for key, want_cmd in (("mic", "mic_gain:0,-40;"),
                              ("comp", "tx_comp:0,0;"),
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
        # ---- DXP: a toggle button mirroring the console DEXP button --------
        sent.clear()
        app._dexp_set(False)
        app._dexp_toggle()
        check("DXP on sends the console threshold + true",
              sent, [f"tx_dexp:0,{int(app.dexp_threshold)},true;"])
        check("DXP button shows on", app.dexp_btn.cget("text"), "on")
        sent.clear()
        app._dexp_toggle()
        check("DXP off sends false",
              sent, [f"tx_dexp:0,{int(app.dexp_threshold)},false;"])
        check("DXP button shows off", app.dexp_btn.cget("text"), "off")

        # the console's own gate button drives ours (threshold kept)
        app.tci_text({"tx_dexp": "0,-55,true"})
        pump(app)
        check("tx_dexp echo turns the button on",
              (app.dexp_on, app.dexp_threshold, app.dexp_btn.cget("text")),
              (True, -55, "on"))
        app.tci_text({"tx_dexp": "0,-55,false"})
        pump(app)
        check("tx_dexp echo turns the button off", app.dexp_on, False)

        # ---- VOX: MiniTCI keys itself from the microphone level ------------
        app._mic_open = lambda: None            # no real device in the test
        app.mic_stream = None
        app.ptt = False
        app.tuning = False
        app._vox_keyed = False
        app._txdsp_set("vox", app.txdsp["vox"]["lo"])      # VOX at the bottom
        app.mic_level_db = -10.0
        sent.clear()
        app._vox_tick()
        check("VOX off never keys", (app.ptt, sent), (False, []))

        app._txdsp_set("vox", -40)                          # armed
        app.mic_level_db = -55.0                            # silence
        sent.clear()
        app._vox_tick()
        check("VOX stays quiet below the threshold", (app.ptt, sent), (False, []))

        app.mic_level_db = -20.0                            # speech
        sent.clear()
        app._vox_tick()
        check("VOX keys on speech",
              (app.ptt, app._vox_keyed, [c for c in sent if c.startswith("trx:")]),
              (True, True, ["trx:0,true,tci;"]))

        # level drops: the carrier holds for the hang time, then releases
        app.mic_level_db = -70.0
        sent.clear()
        app._vox_tick()
        check("VOX holds during the hang time", app.ptt, True)
        app._vox_above_at = time.time() - (app.VOX_HANG_S + 0.2)
        sent.clear()
        app._vox_tick()
        check("VOX releases after the hang time",
              (app.ptt, app._vox_keyed, [c for c in sent if c.startswith("trx:")]),
              (False, False, ["trx:0,false,tci;"]))

        # a manual PTT is never released by the VOX engine
        app._mic_open = lambda: None
        app.ptt_on()
        check("manual PTT keyed", app.ptt, True)
        app.mic_level_db = -70.0
        app._vox_above_at = time.time() - (app.VOX_HANG_S + 0.2)
        app._vox_tick()
        check("VOX leaves a manual PTT alone", app.ptt, True)
        app.ptt_off()

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
              ["mic_gain:0;", "tx_comp:0;", "vox:0;", "tx_dexp:0;"])

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

        # ---- Tune level: the percentage drives the CONSOLE, the tone is fixed
        sent.clear()
        app.tunedrive_entry.delete(0, "end")
        app.tunedrive_entry.insert(0, "20")
        app._tunedrive_entry()
        check("tune drive 20% is sent to the console", sent, ["tune_drive:0,20;"])
        check("tone amplitude stays fixed",
              round(app.tune_amp, 4), round(M.TUNE_TONE_AMP, 4))
        check("entry shows the accepted value",
              app.tunedrive_entry.get().strip(), "20")

        sent.clear()
        app.tunedrive_entry.delete(0, "end")
        app.tunedrive_entry.insert(0, "150")
        app._tunedrive_entry()
        check("out-of-range input is clamped", sent, ["tune_drive:0,100;"])
        sent.clear()
        app.tunedrive_entry.delete(0, "end")
        app.tunedrive_entry.insert(0, "abc")
        app._tunedrive_entry()
        check("bad input is reverted, not sent",
              (sent, app.tunedrive_entry.get().strip()), ([], "100"))

        # the console's own value drives ours
        app.tci_text({"tune_drive": "0,45"})
        pump(app)
        check("tune_drive echo updates the entry",
              (app.tune_drive_pct, app.tunedrive_entry.get().strip()), (45, "45"))
        app.tci_text({"tune_drive": "1,70"})
        pump(app)
        check("tune_drive for rx 1 ignored", app.tune_drive_pct, 45)
        app.tci_text({"drive": "0,35"})
        pump(app)
        check("drive echo also drives the tune level (drive-slider origin)",
              (app.tune_drive_pct, app.tunedrive_entry.get().strip()), (35, "35"))

        # ---- persistence ---------------------------------------------------
        snap = app._settings_snapshot()
        check("settings carry the slider values", isinstance(snap.get("txdsp"), dict)
              and sorted(snap["txdsp"].keys()), ["comp", "mic", "vox"])
        check("settings carry the DXP button state",
              snap.get("dexp_on"), app.dexp_on)
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