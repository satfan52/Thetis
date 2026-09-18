#!/usr/bin/env python3
"""Regression test: the microphone TX stream must be CONTINUOUS.

The server paces TX audio one block per chrono request. Two defects made voice
PTT sound like stuttering/oscillation (while the synthesised Tune tone was
fine):

  1. the PortAudio callback reset the queue read cursor (tx_pos) on every
     callback, so the not-yet-consumed tail of a partially read block was sent
     again - slices of the microphone signal repeated every callback period;
  2. a chrono answered with fewer samples than requested (queue starved) was
     sent as a SHORT frame, i.e. an audible gap.

Both are checked here with a monotonic ramp as the "microphone": the decoded
output must be that ramp, in order, with no repeats and no gaps.

  python test_tx_audio_stream.py
"""
import os
import struct
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import MiniTCI as M

fails = []
STREAM_RATE = 48000
REQ = 1024          # samples the server asks for per chrono
MIC_BLK = 480       # deliberately NOT a divisor of REQ (WASAPI mixer period)


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        fails.append(name)


def decode(frame):
    """Return (requested_length, float32 payload) from a TX_AUDIO frame."""
    words = struct.unpack("<16I", frame[:64])
    length = words[5]
    body = np.frombuffer(frame[64:], dtype="<f4")
    return length, body


def main():
    app = M.MiniTCI()
    frames = []
    try:
        app.withdraw()
        app._loading = True
        app.connected = True
        app.ptt = True
        app.tuning = False
        app.mic_gain = 1.0          # client is unity; Thetis applies the mic gain
        app.mic_rate = STREAM_RATE
        app.send = lambda c: None

        class FakeClient:
            loop = True
            def send_binary(self, b):
                frames.append(bytes(b))
        app.client = FakeClient()

        # monotonic ramp, small enough to survive the output clip, so a
        # duplicate or a gap shows up as a mismatch against the source
        src = (np.arange(0, 300000, dtype=np.float32) / 1_000_000.0)
        pos = 0
        app.tx_audio_q.clear()
        app.tx_pos = 0
        app.tx_underruns = 0
        app._tx_prefilled = False

        def mic_callback():
            """Deliver MIC_BLK samples exactly like the PortAudio callback."""
            nonlocal pos
            blk = src[pos:pos + MIC_BLK].reshape(-1, 1)
            pos += MIC_BLK
            app._mic_cb(blk, len(blk), None, None)

        out = []
        for n in range(40):
            # keep the mic a few blocks ahead of the consumer, as in real use
            while pos < (n + 3) * REQ:
                mic_callback()
            app.chrono_lock.acquire()
            try:
                app.chrono_reqs.append((REQ, STREAM_RATE))
            finally:
                app.chrono_lock.release()
            before = len(frames)
            app.service_tx_audio()
            if len(frames) > before:
                length, body = decode(frames[-1])
                out.append((length, body))

        check("every chrono answered", len(out), 40)
        check("no short frames (all full length)",
              sorted({int(l) for l, _ in out}), [REQ])
        check("no underruns with a fed queue", app.tx_underruns, 0)

        joined = np.concatenate([b for _, b in out])
        check("output length matches what the server asked for",
              len(joined), REQ * 40)
        # the decoded output must be the microphone signal, in order: this single
        # assertion catches both a repeated sample (stutter) and a missing one
        check("output is the microphone signal, in order (no repeats/gaps)",
              bool(np.array_equal(joined, src[:len(joined)])), True)
        check("stream never stalls or repeats",
              float(np.diff(joined).min()) > 0.0, True)

        # ---- a chrono arriving before the microphone has buffered anything
        # must still be answered with a FULL silent block (no short frame, no
        # stutter at the start of a transmission)
        frames.clear()
        app.tx_audio_q.clear()
        app.tx_pos = 0
        app.tx_underruns = 0
        app._tx_prefilled = False
        app.chrono_lock.acquire()
        try:
            app.chrono_reqs.append((REQ, STREAM_RATE))
        finally:
            app.chrono_lock.release()
        app.service_tx_audio()
        check("prefill chrono still answered", len(frames), 1)
        if frames:
            length, body = decode(frames[0])
            check("prefill frame is a full silent block",
                  (int(length), len(body), bool(np.all(body == 0.0))),
                  (REQ, REQ, True))
        check("prefill is not counted as an underrun", app.tx_underruns, 0)
        check("still waiting for the prebuffer", app._tx_prefilled, False)

        # ---- once streaming, a starved chrono is padded (never short)
        app._tx_prefilled = True
        frames.clear()
        app.chrono_lock.acquire()
        try:
            app.chrono_reqs.append((REQ, STREAM_RATE))
        finally:
            app.chrono_lock.release()
        app.service_tx_audio()
        check("starved chrono answered after prefill", len(frames), 1)
        if frames:
            length, body = decode(frames[0])
            check("starved frame is full length", (int(length), len(body)),
                  (REQ, REQ))
        check("underrun counted", app.tx_underruns, 1)

        # ---- latency bound: a long backlog drops the OLDEST audio
        frames.clear()
        app.tx_audio_q.clear()
        app.tx_pos = 0
        app._tx_prefilled = True
        for k in range(20):        # 9600 samples > the 8-block latency bound
            app.tx_audio_q.append(np.full(480, float(k) / 100.0, dtype=np.float32))
        app.chrono_lock.acquire()
        try:
            app.chrono_reqs.append((REQ, STREAM_RATE))
        finally:
            app.chrono_lock.release()
        app.service_tx_audio()
        length, body = decode(frames[0])
        # the oldest blocks must have been dropped: block 0 (value 0.00) can no
        # longer be the head, so the first sample is from a later block
        check("backlog is bounded, not sent raw",
              (int(length), float(body[0]) >= 0.02), (REQ, True))
        queued = sum(len(b) for b in app.tx_audio_q) - app.tx_pos
        check("queue length stays within the bound",
              queued <= app.TX_MAX_QUEUE_BLOCKS * REQ, True)

        # ---- the callback must not disturb the read cursor
        app.tx_audio_q.clear()
        app.tx_audio_q.append(np.arange(1000, dtype=np.float32))
        app.tx_pos = 300
        app._mic_cb(np.zeros((MIC_BLK, 1), dtype=np.float32), MIC_BLK, None, None)
        check("callback leaves tx_pos alone", app.tx_pos, 300)

        # ---- a device running at 44.1k must be resampled, not mislabelled
        app.tx_audio_q.clear()
        app.tx_pos = 0
        app.mic_rate = 44100.0
        app.tx_audio_q.append(np.zeros(4410, dtype=np.float32))
        app.chrono_lock.acquire()
        try:
            app.chrono_reqs.append((REQ, STREAM_RATE))
        finally:
            app.chrono_lock.release()
        frames.clear()
        app.service_tx_audio()
        length, body = decode(frames[0])
        check("44.1k mic output is resampled to the requested count",
              (int(length), len(body)), (REQ, REQ))
    finally:
        app.destroy()

    print()
    if fails:
        print(f"{len(fails)} FAILED: {fails}")
        return 1
    print("all TX-audio stream checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())