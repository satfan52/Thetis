#!/usr/bin/env python3
"""Regression test for the MiniTCI TX path (no RF: nothing here keys a radio).

Covers the three defects behind "Tune and PTT are broken, Thetis stays in
reception, no audio reaches Thetis":
  1. the key command must carry the 'tci' marker the full TCI server requires
     before it will consume TCI TX audio (without it cmaster discards the audio);
  2. PTT while disconnected must not pretend to transmit;
  3. a dropped session must reconnect by itself (the app was left sitting
     'disconnected', so every PTT/Tune press silently did nothing).
Plus: the server's MOX/TUN broadcast must drive BOTH TX buttons.

  python test_tx_path.py
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


def _call_text(app, d, pump_s=0.4):
    """Feed a server text frame through the shipped handler and let the UI poll
    drain it (frames reach the app through a queue, not synchronously)."""
    app.tci_text(d)
    t0 = time.time()
    while time.time() - t0 < pump_s:
        app.update()
        time.sleep(0.01)


def main():
    app = M.MiniTCI()
    try:
        app.withdraw()
        app._loading = True
        sent = []
        app.send = lambda c: sent.append(c)

        # ---- 1. PTT while disconnected ----------------------------------
        app.connected = False
        sent.clear()
        app.ptt_on()
        check("disconnected PTT sends nothing", sent, [])
        check("disconnected PTT does not set the flag", app.ptt, False)

        # ---- 2. connected PTT carries the tci marker ---------------------
        app.connected = True
        sent.clear()
        app.ptt_on()
        check("PTT key command", [c for c in sent if c.startswith("trx:")],
              ["trx:0,true,tci;"])
        check("PTT flag set", app.ptt, True)
        sent.clear()
        app.ptt_off()
        check("PTT release command", [c for c in sent if c.startswith("trx:")],
              ["trx:0,false,tci;"])

        # ---- 3. Tune: trx (audio ownership) + Thetis's own TUN -----------
        app.ptt = False
        app.tuning = False
        app.tune_active = False
        sent.clear()
        app.tune_toggle()
        check("Tune claims TX audio", [c for c in sent if c.startswith("trx:")],
              ["trx:0,true,tci;"])
        check("Tune drives Thetis's TUN",
              [c for c in sent if c.startswith("tune:")], ["tune:0,true;"])
        check("Tune flag set", app.tuning, True)
        check("Tune button lit", app.tune_btn.cget("bg"), M.C["red"])
        check("Tune also lights PTT (Thetis TUN asserts MOX)",
              app.ptt_btn.cget("bg"), M.C["red"])
        sent.clear()
        app.tune_stop()
        check("Tune flag cleared", app.tuning, False)
        check("Tune release drives Thetis's TUN off",
              [c for c in sent if c.startswith("tune:")], ["tune:0,false;"])
        check("Tune button dark again", app.tune_btn.cget("bg"), "#f7e6c8")

        # PTT must not light the Tune button (independence both ways)
        app.ptt = False
        app.tuning = False
        app.tune_active = False
        sent.clear()
        app.ptt_on()
        check("PTT button lit", app.ptt_btn.cget("bg"), M.C["red"])
        check("PTT does NOT light Tune", app.tune_btn.cget("bg"), "#f7e6c8")
        check("PTT sends no tune command",
              [c for c in sent if c.startswith("tune:")], [])
        app.ptt_off()

        # ---- 4. server broadcasts drive the two buttons independently ----
        app.ptt = False
        app.tune_active = False
        _call_text(app, {"trx": "0,true"})
        check("trx echo sets PTT flag", app.ptt, True)
        check("trx echo lights PTT button", app.ptt_btn.cget("bg"), M.C["red"])
        check("trx echo leaves TUNE dark", app.tune_btn.cget("bg"), "#f7e6c8")
        check("trx echo shows TX", app.tx_lbl.cget("text").startswith("TX"), True)
        _call_text(app, {"trx": "0,false"})
        check("trx release clears the label", app.tx_lbl.cget("text"), "RX")

        # Thetis's own TUN lights TUNE; the key that comes with it must not
        # light PTT (that was the reported 'PTT kicks in with Tune')
        _call_text(app, {"tune": "0,true"})
        check("tune echo sets the tune flag", app.tune_active, True)
        check("tune echo lights TUNE", app.tune_btn.cget("bg"), M.C["red"])
        _call_text(app, {"trx": "0,true"})
        check("key from a tune carrier shows PTT too",
              app.ptt_btn.cget("bg"), M.C["red"])
        check("tune carrier shows TX tune",
              app.tx_lbl.cget("text").endswith("tune"), True)
        _call_text(app, {"tune": "0,false"})
        _call_text(app, {"trx": "0,false"})
        check("everything released", app.tx_lbl.cget("text"), "RX")

        # frames addressed to another receiver must not touch us
        app.ptt = False
        app.tune_active = False
        _call_text(app, {"trx": "1,true"})
        check("trx for rx 1 ignored", app.ptt, False)
        _call_text(app, {"tune": "1,true"})
        check("tune for rx 1 ignored", app.tune_active, False)

        # ---- 5. auto-reconnect after a drop -------------------------------
        app.connected = True
        app._reconnect_tries = 0
        app._manual_disconnect = False
        scheduled = []
        real_after = app.after                      # the poll chain uses after()
        app.after = lambda ms, fn=None, *a: scheduled.append((ms, fn))
        app._set_state("disconnected")
        app.after = real_after                      # restore before later sections
        check("drop schedules a reconnect", len(scheduled), 1)
        check("reconnect delay", scheduled[0][0], app.RECONNECT_DELAY_MS)

        # a manual disconnect must NOT auto-reconnect
        app.connected = True
        app._manual_disconnect = True
        scheduled.clear()
        app._set_state("disconnected")
        check("manual disconnect does not reconnect", scheduled, [])

        # attempts are bounded
        app._manual_disconnect = False
        app._reconnect_tries = app.RECONNECT_MAX_TRIES
        scheduled.clear()
        app._set_state("disconnected")
        check("reconnect gives up after the cap", scheduled, [])

        # ---- 7. PTT is a TOGGLE (Thetis MOX parity), not hold-to-talk --------
        app.connected = True
        binds = [app.ptt_btn.bind("<ButtonPress-1>"), app.ptt_btn.bind("<ButtonRelease-1>")]
        check("PTT has no press/release bindings", [b for b in binds if b], [])
        check("PTT button carries a command (one trigger)",
              bool(app.ptt_btn.cget("command")), True)
        app.ptt = False
        app.tuning = False
        app.tune_active = False
        sent.clear()
        app.ptt_toggle()                       # click 1 = key
        check("toggle keys", (app.ptt, [c for c in sent if c.startswith("trx:")]),
              (True, ["trx:0,true,tci;"]))
        sent.clear()
        app.ptt_toggle()                       # click 2 = release
        check("toggle releases", (app.ptt, [c for c in sent if c.startswith("trx:")]),
              (False, ["trx:0,false,tci;"]))

        # ---- 8. stuck-key watchdog -----------------------------------------
        app.connected = True
        app.ptt = False
        app.tuning = False
        app.tune_active = False
        app._key_false_since = None
        app._key_requested = False
        app.ptt_on()                           # we ask the server to key
        check("key intent recorded", app._key_requested, True)
        _call_text(app, {"trx": "0,false"})    # ...but the server reports RX
        check("watchdog armed", app._key_false_since is not None, True)
        app._key_false_since = time.time() - 3.0
        app._check_key_watchdog()
        check("watchdog clears a stale key", (app.ptt, app._key_requested),
              (False, False))
        check("watchdog keeps the mute tail", app._tx_mute_until > time.time(), True)

        # a fresh key echo disarms it
        app.ptt_on()
        app._key_false_since = time.time()
        _call_text(app, {"trx": "0,true"})
        check("key echo disarms the watchdog", app._key_false_since, None)
        app.ptt_off()
        app._tx_visuals()

        # ---- 9. Tune mirrors Thetis: TUN also asserts MOX -------------------
        app.connected = True
        app.ptt = False
        app.tuning = False
        app.tune_active = False
        app._tx_visuals()
        sent.clear()
        app.tune_toggle()
        check("Tune lights TUNE", app.tune_btn.cget("bg"), M.C["red"])
        check("Tune also lights PTT (Thetis asserts MOX with TUN)",
              app.ptt_btn.cget("bg"), M.C["red"])
        app.tune_stop()
        check("both released", (app.tune_btn.cget("bg"), app.ptt_btn.cget("bg")),
              ("#f7e6c8", "#f4d7d4"))

        # ---- 6. AGC token maps exist and match the server contract ---------
        # _agc_mode_to_tci raised AttributeError on connect (AGC_MODES_TCI was
        # never defined), which aborted _set_state("connected") mid-flight.
        check("Fixed maps to the FIXD token",
              app._agc_mode_to_tci("Fixed"), "off")
        check("Med maps to the MED token",
              app._agc_mode_to_tci("Med"), "normal")
        check("every UI name maps",
              [app._agc_mode_to_tci(n) for n in
               ("Long", "Slow", "Fast", "Custom")],
              ["long", "slow", "fast", "custom"])
        check("tokens map back to UI names",
              [app._agc_mode_from_tci(t) for t in
               ("off", "long", "slow", "normal", "fast", "custom")],
              ["Fixed", "Long", "Slow", "Med", "Fast", "Custom"])
        # the full connect burst must run through without raising
        app.connected = False
        app._is_full_tci = True          # port 50001 path
        raised = None
        try:
            app._set_state("connected")
        except Exception as e:                     # noqa: BLE001
            raised = f"{type(e).__name__}: {e}"
        check("connect burst raises nothing", raised, None)
        check("connect burst pushed the AGC state",
              [c for c in sent if c.startswith("agc_")][-2:],
              # derived from the app's own state: the saved settings file is live
              # state, so a fixed expectation would fail on whatever AGC the last
              # real session used
              [f"agc_mode:0,{app._agc_mode_to_tci(app.agc_var.get())};",
               f"agc_gain:0,{int(app.agc_gain_var.get())};"])
        app.connected = False
        # a successful connect resets the counter
        app._reconnect_tries = 5
        app._set_state("connected")
        check("connected resets the counter", app._reconnect_tries, 0)
        app.connected = False
    finally:
        app.destroy()

    print()
    if fails:
        print(f"{len(fails)} FAILED: {fails}")
        return 1
    print("all TX-path checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())