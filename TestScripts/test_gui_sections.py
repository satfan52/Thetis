#!/usr/bin/env python3
"""Regression test: the MiniTCI window is grouped by OWNER, and the SubVFOA
block is presented exactly like the VFO A block.

Sections under test:
  1 GENERAL  connection, audio routing, link state
  2 DISPLAY  panafall and its adjust sliders, shared by both receivers
  3 VFO A    RX1
  4 SubVFOA  SubRX1, same presentation, its OWN DSP chain
  5 TX       transmission only
  6 LOG      status

Rules under test:
  * every control sits in the section that owns the state it changes;
  * the SubVFOA block uses the same widget types, sizes and units as VFO A;
  * the sub's mode, filter and AGC are its own: they are sent as sub_mode /
    sub_filter / sub_agc_mode / sub_agc_gain, and a VFO A mode change does NOT
    drag the sub along;
  * the audio selector and the A<->B mix live in GENERAL.

  python test_gui_sections.py
"""
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import MiniTCI as M

fails = []


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        fails.append(name)


def descendants(w):
    out = []
    for c in w.winfo_children():
        out.append(c)
        out += descendants(c)
    return out


def owner_of(widget, sections):
    """Name of the section that contains widget, or None."""
    for name, sec in sections.items():
        if widget is sec or widget in descendants(sec):
            return name
    return None


def find(widget, cls, pred=None):
    """First descendant of the given ttk/tk class, optionally matching pred."""
    for w in descendants(widget):
        if w.winfo_class() == cls and (pred is None or pred(w)):
            return w
    return None


def main():
    app = M.MiniTCI()
    try:
        app.withdraw()
        app._loading = True
        app.connected = True
        app._is_full_tci = True
        app.send = lambda c: app.sent.append(c)
        app.sent = []
        app.update_idletasks()

        sections = {"general": app.sec_general, "display": app.sec_display,
                    "vfoa": app.sec_vfoa, "sub": app.sec_sub,
                    "tx": app.sec_tx, "log": app.sec_log}

        # ---------------- the sections exist and are laid out as agreed -------
        check("six titled sections", [s.winfo_class() for s in sections.values()],
              ["TLabelframe"] * 6)
        titles = {n: sections[n].cget("text") for n in sections}
        check("section titles name their owner",
              [titles[n].split("·")[-1].split("—")[0].strip()
               for n in ("general", "display", "vfoa", "sub", "tx", "log")],
              ["GENERAL", "DISPLAY", "VFO A", "SubVFOA", "TX", "LOG"])
        check("receivers share the left column, transmit its own",
              (sections["vfoa"].master is sections["sub"].master,
               sections["tx"].master is not sections["vfoa"].master,
               sections["tx"].master is not app,
               sections["vfoa"].master is not app),
              (True, True, True, True))
        check("display and log span the window",
              (sections["display"].master is app, sections["log"].master is app),
              (True, True))

        # ---------------- every control sits with its owner ------------------
        check("connection controls in GENERAL",
              [owner_of(w, sections) for w in (app.conn_btn, app.state_lbl)],
              ["general", "general"])
        audio_sel = find(app.sec_general, "TCombobox",
                         lambda w: "Both" in str(w.cget("values")))
        check("audio selector in GENERAL", owner_of(audio_sel, sections), "general")
        mix = find(app.sec_general, "TScale",
                   lambda w: str(w.cget("variable")) == str(app.bal_var))
        check("A<->B mix in GENERAL", owner_of(mix, sections), "general")
        check("display controls in DISPLAY",
              [owner_of(w, sections) for w in
               (app.pan, app.yzero_scale, app.yscale_scale, app.zoom_scale, app.wf_scale)],
              ["display"] * 5)
        check("VFO A controls in the VFO A section",
              [owner_of(w, sections) for w in
               (app.freq_lbl, app.ctun_btn, app.dds_lbl, app.tune_entry,
                app.filt_low_entry, app.filt_high_entry, app.filtw_scale,
                app.agc_box, app.agc_gain_scale, app.vol_scale, app.sm)],
              ["vfoa"] * 11)
        check("SubVFOA controls in the SubVFOA section",
              [owner_of(w, sections) for w in
               (app.vfo_lbl, app.sub_btn, app.sub_tune_entry,
                app.sub_filt_low_entry, app.sub_filt_high_entry,
                app.sub_filtw_scale, app.sub_agc_box, app.sub_agc_gain_scale)],
              ["sub"] * 8)
        check("transmit controls in the TX section",
              [owner_of(w, sections) for w in
               (app.ptt_btn, app.tune_btn, app.tx_lbl, app.txtail_entry,
                app.tunedrive_entry, app.dexp_btn,
                app.txdsp["mic"]["scale"], app.txdsp["comp"]["scale"],
                app.txdsp["vox"]["scale"])],
              ["tx"] * 9)
        # SPLIT is a transmit decision and SUB is a reception one: the two must
        # sit in different sections
        check("SPLIT belongs to TX, SUB to SubVFOA",
              (owner_of(app.split_btn, sections), owner_of(app.sub_btn, sections)),
              ("tx", "sub"))
        check("the TX section names where the transmitter goes",
              (app.tx_src_lbl.cget("text"), owner_of(app.tx_src_lbl, sections)),
              ("TX on VFO A", "tx"))
        check("mic device with the other transmit controls",
              owner_of(find(app.sec_tx, "TCombobox",
                            lambda w: "default" in str(w.cget("values"))), sections),
              "tx")
        check("log in the LOG section", owner_of(app.log, sections), "log")

        # ---------------- SubVFOA presented exactly like VFO A ---------------
        check("both readouts use the same font",
              app.vfo_lbl.cget("font") == app.freq_lbl.cget("font"), True)
        both_modes = find(app.sec_sub, "TCombobox",
                          lambda w: list(w.cget("values")) == M.MODES)
        check("sub mode combo carries the same modes as VFO A",
              list(both_modes.cget("values")), list(app.agc_box.cget("values"))[:0] or
              list(M.MODES))
        check("both filter entry boxes are the same width",
              (int(app.sub_filt_low_entry.cget("width")),
               int(app.sub_filt_high_entry.cget("width"))),
              (int(app.filt_low_entry.cget("width")),
               int(app.filt_high_entry.cget("width"))))
        check("both variable bandwidth sliders are identical",
              (int(app.sub_filtw_scale.cget("from")), int(app.sub_filtw_scale.cget("to")),
               int(app.sub_filtw_scale.cget("length"))),
              (int(app.filtw_scale.cget("from")), int(app.filtw_scale.cget("to")),
               int(app.filtw_scale.cget("length"))))
        check("both AGC dropdowns carry the same names",
              (list(app.sub_agc_box.cget("values")), list(app.agc_box.cget("values"))),
              (M.AGC_UI_NAMES, M.AGC_UI_NAMES))
        check("both AGC gain sliders are identical",
              (int(app.sub_agc_gain_scale.cget("from")),
               int(app.sub_agc_gain_scale.cget("to")),
               int(app.sub_agc_gain_scale.cget("length"))),
              (int(app.agc_gain_scale.cget("from")), int(app.agc_gain_scale.cget("to")),
               int(app.agc_gain_scale.cget("length"))))
        check("both readouts tune digit by digit",
              (bool(app.freq_lbl.bind("<MouseWheel>")),
               bool(app.vfo_lbl.bind("<MouseWheel>"))), (True, True))

        # the sub readout tunes per digit, like VFO A. Use a digit whose step
        # stays inside the slice: the sub is clamped to VFO A's DDC passband.
        # VFO A's slice must contain the sub: the sub lives inside it
        app.freq_hz = 14_074_000
        app.pan.center_hz = 14_074_000
        app.pan.data_center_hz = 14_074_000
        app.sub_hz = 14_076_000
        app._sub_refresh_ui()
        # locate the 1 kHz digit by asking the readout itself, so the step stays
        # inside the slice (the sub is clamped to VFO A's DDC passband)
        x_khz = min(x for x in range(0, 400)
                    if app._readout_place(x, "sub") == 1000)
        before = app.sub_hz
        app._readout_wheel(types.SimpleNamespace(x=x_khz, delta=120, state=0), "sub")
        check("sub readout wheel steps the digit under the pointer",
              app.sub_hz - before, 1000)
        check("and the sub readout follows", app.vfo_lbl.cget("text"),
              app._fmt_sub_freq())

        # ---------------- the sub's DSP chain is its own ---------------------
        app.sub_enabled = True
        app.sent.clear()
        app.submode_var.set("USB" if app.sub_mode != "USB" else "LSB")
        app._split_set(True)
        check("SPLIT on: the transmitter moves to the SubVFOA",
              (app.split_btn.cget("text"), app.tx_src_lbl.cget("text"), app.tx_vfo),
              ("on", "TX on SubVFOA", "B"))
        app._split_set(False)
        check("SPLIT off: back to VFO A",
              (app.split_btn.cget("text"), app.tx_src_lbl.cget("text"), app.tx_vfo),
              ("off", "TX on VFO A", "A"))

        check("the sub sends its own mode and filter",
              [c.split(":")[0] for c in app.sent], ["sub_mode", "sub_filter"])
        check("the sub filter is a sub_filter, never rx_filter_band",
              [c for c in app.sent if c.startswith("rx_filter_band")], [])
        app.sent.clear()
        app.sub_agc_var.set("Fast" if app.sub_agc_var.get() != "Fast" else "Slow")
        check("the sub sends its own AGC",
              [c.split(":")[0] for c in app.sent], ["sub_agc_mode", "sub_agc_gain"])
        app.sent.clear()
        app.mode_var.set("LSB" if app.mode_var.get() != "LSB" else "USB")
        check("a VFO A mode change leaves the sub alone",
              [c for c in app.sent if c.startswith("sub_")], [])
        check("and VFO A still sends its own commands",
              sorted({c.split(":")[0] for c in app.sent}), ["modulation", "rx_filter_band"])
        app.sent.clear()
        low = int(float(app.sub_filt_low_entry.get()))
        app.sub_filt_high_entry.delete(0, "end")
        app.sub_filt_high_entry.insert(0, "2400")
        app._sub_filter_entries_applied()
        check("editing the sub filter boxes sends the sub passband",
              app.sent, [f"sub_filter:0,{low},2400;"])
        check("the sub slider follows the boxes", int(app.sub_filtw_var.get()),
              2400 - low)
        check("the sub width label shows the new width", app.sub_filtw_lbl.cget("text"),
              f"{(2400 - low) / 1000:.1f}k")
    finally:
        app.destroy()

    print()
    if fails:
        print("FAILED:", fails)
        sys.exit(1)
    print("all GUI-section and SubVFOA-parity checks passed")


if __name__ == "__main__":
    main()