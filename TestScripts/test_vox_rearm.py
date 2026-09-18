#!/usr/bin/env python3
"""Regression test: VOX must keep working transmission after transmission.

Reported: "it will work the first time I speak (tested at -37 dB), then when I try
again it does not work any longer the second time I speak unless I move the cursor
up to down a bit."

Cause: ptt_off() closed the microphone stream unconditionally, and with VOX armed
the microphone IS the detector's input. After the first over the level stayed
frozen at -140 dB, so no later speech could key; nudging the slider re-opened the
device through _vox_mic_keep() and it worked once more - the "unpredictable"
behaviour.

Rules under test:
  * releasing a VOX transmission leaves the microphone open, same device object;
  * a second and third over key again with nothing touched;
  * with VOX off the device IS released (no permanently held input);
  * the poll loop re-opens a lost input while VOX is armed;
  * a device that fails to open is not retried 20 times a second.

  python test_vox_rearm.py
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


class FakeStream:
    """Stands in for a sounddevice InputStream."""

    def __init__(self):
        self.closed = False
        self.stopped = False

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def pump(app, secs=0.15):
    t0 = time.time()
    while time.time() - t0 < secs:
        app.update()
        time.sleep(0.01)


def main():
    app = M.MiniTCI()
    try:
        app.withdraw()
        app._loading = True
        app.connected = True
        app._is_full_tci = True
        app.send = lambda c: app.sent.append(c)
        app.sent = []
        app.client = type("C", (), {"loop": False,
                                    "send_binary": lambda self, b: None})()
        app.service_tx_audio = lambda: None      # the stream test owns that path

        opened = []
        real_open = M.MiniTCI._mic_open      # the real device path, for the tests below

        def fake_open():
            if app.mic_stream is None:
                app.mic_stream = FakeStream()
                opened.append(app.mic_stream)

        app._mic_open = fake_open

        # ---- arm VOX at -40 dB ------------------------------------------
        app._txdsp_set("vox", -40)
        app._vox_mic_keep()
        check("arming VOX opens the microphone", len(opened), 1)
        check("the detector starts armed", app._vox_threshold(), -40.0)

        # ---- three consecutive overs ------------------------------------
        def feed(level):
            """The detector's input, as the microphone callback would provide it.

            With no open input there is no callback, so the level does NOT
            change - exactly why a closed device left VOX unable to key again.
            """
            if app.mic_stream is None or app.mic_stream.closed:
                return False
            app.mic_level_db = level
            return True

        keyed, released, measured = [], [], []
        for over in (1, 2, 3):
            measured.append(feed(-20.0))                # speech
            app.sent.clear()
            app._vox_tick()
            keyed.append("trx:0,true,tci;" in app.sent)

            feed(-70.0)                                 # silence, past the hang
            app._vox_above_at = time.time() - (app.VOX_HANG_S + 0.2)
            app.sent.clear()
            app._vox_tick()
            released.append("trx:0,false,tci;" in app.sent)

            if over == 1:
                check("releasing keeps the same microphone open",
                      (app.mic_stream is None, app.mic_stream is opened[0]),
                      (False, True))
                check("no extra device opened during the over", len(opened), 1)

        check("the microphone was open for every over", measured, [True, True, True])
        check("every over keys the transmitter", keyed, [True, True, True])
        check("every over releases", released, [True, True, True])
        check("still one microphone after three overs", len(opened), 1)

        # ---- the poll loop puts a lost input back -----------------------
        app.mic_stream = None
        pump(app, 0.25)
        check("the poll loop re-opens a lost input while VOX is armed",
              app.mic_stream is not None, True)

        # ---- VOX off releases the device --------------------------------
        app._txdsp_set("vox", app.txdsp["vox"]["lo"])
        app._vox_mic_keep()
        check("VOX off closes the microphone", app.mic_stream, None)
        check("and the device was really closed", opened[-1].closed, True)

        # ---- a failing device is not retried at the poll rate ------------
        attempts = []

        class BadSd:
            class InputStream:
                def __init__(self, **kw):
                    attempts.append(kw)
                    raise RuntimeError("no device")

        real_sd = M.sd
        M.sd = BadSd
        try:
            app.mic_stream = None
            app._mic_retry_after = 0.0
            real_open(app)                 # the real path, with a failing device
            real_open(app)
            real_open(app)
            check("a dead device is tried once, not per poll cycle",
                  len(attempts), 1)
            app._mic_retry_after = 0.0
            real_open(app)
            check("and is retried after the back-off", len(attempts), 2)
            check("a failed open leaves no half-open stream", app.mic_stream, None)
        finally:
            M.sd = real_sd
    finally:
        app.destroy()

    print()
    if fails:
        print("FAILED:", fails)
        sys.exit(1)
    print("all VOX re-arm checks passed")


if __name__ == "__main__":
    main()