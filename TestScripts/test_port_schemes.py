#!/usr/bin/env python3
"""Port-scheme regression test: the commands MiniTCI SENDS must match the
connected server's vocabulary.

Two schemes share one client:
  * port 50001   = full TCIServer   (rx_balance, vfoasub, rx_channel_enable, rx_ctun_ex)
  * 50002-50008  = headless server  (sub_balance, vfo:1,0, subrx, sub_mode, sub_filter, ctun)

Every send is captured with a stub and asserted per port. Extend it whenever a
new port-dependent command is added.

  python test_port_schemes.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import MiniTCI as M

fails = []


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n        got  {got}\n        want {want}")
    if not ok:
        fails.append(name)


def capture(app, fn, *a, **kw):
    sent = []
    real = app.send
    app.send = lambda s: sent.append(s)
    try:
        fn(*a, **kw)
    finally:
        app.send = real
    return sent


def keys(sent):
    return sorted({s.split(":", 1)[0] for s in sent})


def main():
    app = M.MiniTCI()
    try:
        app.withdraw()
        app._loading = True
        app.connected = True
        app.freq_hz = 14_074_000
        app.sub_hz = 14_080_000
        app.sub_enabled = True
        app.sub_mode = "USB"
        app.sub_filt = (150, 2850)
        app.pan.center_hz = 14_074_000.0
        app.pan.data_center_hz = 14_074_000.0

        for full, port, tag in ((True, 50001, "50001"), (False, 50003, "headless")):
            app._is_full_tci = full

            # --- sub frequency tune -----------------------------------------
            app.sub_enabled = True
            sent = capture(app, app._sub_tune_to, 14_080_000)
            want = ["vfoasub:0,14080000;"] if full else ["vfo:1,0,14080000;"]
            check(f"{tag} sub tune vocabulary", sent, want)

            # --- VFO A tune: identical on both schemes ----------------------
            sent = capture(app, app.tune_to, 14_074_000)
            check(f"{tag} VFO A always sends vfo:0,0", any(
                s.startswith("vfo:0,0,") for s in sent), True)
            check(f"{tag} VFO A sub-token matches the scheme",
                  ("vfoasub" in keys(sent)) == full, True)

            # --- sub enable ---------------------------------------------------
            app.sub_enabled = False
            sent = capture(app, app._sub_toggle)
            if full:
                check(f"{tag} sub enable uses rx_channel_enable",
                      "rx_channel_enable" in keys(sent), True)
                check(f"{tag} sub enable sends vfoasub", "vfoasub" in keys(sent), True)
                check(f"{tag} sub enable must NOT send subrx", "subrx" in keys(sent), False)
                check(f"{tag} sub enable uses rx_balance", "rx_balance" in keys(sent), True)
            else:
                check(f"{tag} sub enable uses subrx", "subrx" in keys(sent), True)
                check(f"{tag} sub enable sends vfo:1", any(
                    s.startswith("vfo:1,0,") for s in sent), True)
                check(f"{tag} sub enable must NOT send rx_channel_enable",
                      "rx_channel_enable" in keys(sent), False)
                check(f"{tag} sub enable sends sub_mode/sub_filter",
                      {"sub_mode", "sub_filter"}.issubset(set(keys(sent))), True)

            # --- balance / audio selection ------------------------------------
            # ONE command on BOTH schemes: 50001 implements sub_balance with the
            # same per-channel-gain law as the headless server.
            app.sub_enabled = True
            app.audio_sel = "both"
            sent = capture(app, app._bal_changed, 0.25)
            check(f"{tag} balance slider token", keys(sent), ["sub_balance"])

            sent = capture(app, app._apply_audio_selection, True)
            check(f"{tag} audio selection token", keys(sent), ["sub_balance"])
            check(f"{tag} audio selection value = balance",
                  sent[0].startswith("sub_balance:0,"), True)

            # --- CTUN ---------------------------------------------------------
            app.ctun_var.set(True)
            sent = capture(app, app._ctun_toggled)
            want_key = "rx_ctun_ex" if full else "ctun"
            check(f"{tag} CTUN token", want_key in keys(sent), True)
            app.ctun_var.set(False)

            # --- cross-scheme leakage: no headless-only token on 50001 --------
            if full:
                rec = []
                real = app.send
                app.send = lambda s: rec.append(s)
                app.tune_to(14_074_000)
                app._sub_tune_to(14_080_000)
                app._bal_changed(0.4)
                app.send = real
                for tok in ("subrx", "sub_mode", "sub_filter"):
                    check(f"50001 never sends headless-only {tok}", any(
                        s.startswith(tok + ":") for s in rec), False)
    finally:
        try:
            app.destroy()
        except Exception:
            pass

    print()
    if fails:
        print(f"FAILED: {len(fails)} -> {fails}")
        return 1
    print("all port-scheme checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
