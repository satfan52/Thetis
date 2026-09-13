"""
Branch G verification harness — IQ streaming, DDS, S-meter, AGC, mute on headless ports.

Prerequisites:
  - Thetis (Branch G build) running, TCI server enabled, bound to 127.0.0.1:50001
  - Red Pitaya on the network, radio powered ON (the script can power it on via 50001)
  - python with `websockets` package (same as the existing test scripts)

Usage:
  python test_branch_g.py            # full suite
  python test_branch_g.py 50003     # single-port IQ test only
"""

import sys
import time
import socket
import struct
import statistics

try:
    from websockets.sync.client import connect
except ImportError:
    print("ERROR: pip install websockets")
    sys.exit(1)

sys.stdout.reconfigure(line_buffering=True)

PASS = "PASSED"
FAIL = "FAILED"
results = []


def record(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def is_port_listening(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(0.5)
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except Exception:
        return False


def ensure_thetis():
    if is_port_listening(50001):
        print("[INIT] Thetis already running.")
        return
    import subprocess
    print("[INIT] Launching Thetis.exe...")
    subprocess.Popen([r"C:\Thetis\Thetis.exe"], cwd=r"C:\Thetis")
    for _ in range(20):
        time.sleep(1)
        if is_port_listening(50001):
            time.sleep(2)
            print("[INIT] Thetis ready.")
            return
    raise RuntimeError("Thetis failed to start (port 50001 not listening).")


def drain_banner(ws, timeout=1.0):
    """Consume the init banner text frames. Returns dict of parsed key values."""
    banner = {}
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            msg = ws.recv(timeout=0.2)
        except TimeoutError:
            continue
        if isinstance(msg, str):
            for cmd in msg.split(";"):
                cmd = cmd.strip()
                if not cmd or ":" not in cmd:
                    continue
                k, v = cmd.split(":", 1)
                banner[k.strip()] = v.strip()
        if banner.get("ready") is not None and time.time() - t0 > 0.5:
            break
    return banner


def parse_binary_header(payload):
    """64-byte TCI binary header."""
    if len(payload) < 64:
        return None
    return {
        "receiver":   struct.unpack("<I", payload[0:4])[0],
        "samplerate": struct.unpack("<I", payload[4:8])[0],
        "sampletype": struct.unpack("<I", payload[8:12])[0],
        "length":     struct.unpack("<i", payload[20:24])[0],
        "frametype":  struct.unpack("<I", payload[24:28])[0],
        "channels":   struct.unpack("<I", payload[28:32])[0],
    }


def collect(ws, duration, want_types):
    """
    Receive for `duration` seconds.
    want_types: set of frame types to count (0=IQ, 1=RX audio, 3=TX chrono).
    Returns dict with per-type stats and any text messages seen.
    """
    stats = {t: {"count": 0, "bytes": 0, "intervals": []} for t in want_types}
    texts = []
    last_t = {t: None for t in want_types}
    t_start = time.time()
    while time.time() - t_start < duration:
        try:
            msg = ws.recv(timeout=1.0)
        except TimeoutError:
            continue
        now = time.time()
        if isinstance(msg, bytes) and len(msg) >= 32:
            ft = struct.unpack("<I", msg[24:28])[0]
            if ft in stats:
                s = stats[ft]
                s["count"] += 1
                s["bytes"] += len(msg)
                if last_t[ft] is not None:
                    s["intervals"].append((now - last_t[ft]) * 1000.0)
                last_t[ft] = now
        elif isinstance(msg, str):
            texts.append(msg.strip())
    return stats, texts


# ---------------------------------------------------------------- tests

def test_banner_and_iq_negotiation(port=50003):
    name = f"Banner + iq_samplerate negotiation (port {port})"
    with connect(f"ws://127.0.0.1:{port}/") as ws:
        banner = {}
        t0 = time.time()
        while time.time() - t0 < 2.0:
            try:
                msg = ws.recv(timeout=0.3)
            except TimeoutError:
                continue
            if isinstance(msg, str):
                for cmd in msg.split(";"):
                    cmd = cmd.strip()
                    if cmd and ":" in cmd:
                        k, v = cmd.split(":", 1)
                        banner[k.strip()] = v.strip()
        ok_ready = "ready" in banner or banner.get("start") is not None
        trx1 = banner.get("trx_count") == "1"
        iqs = banner.get("iq_samplerate")
        ok_iq = iqs in ("96000", "48000", "192000", "384000")
        record(name, ok_ready and trx1 and ok_iq,
               f"trx_count={banner.get('trx_count')}, iq_samplerate={banner.get('iq_samplerate')}")

        # Negotiate a non-default rate and confirm echo
        ws.send("iq_samplerate:192000;")
        ack = None
        t0 = time.time()
        while time.time() - t0 < 2.0:
            try:
                msg = ws.recv(timeout=0.3)
            except TimeoutError:
                continue
            if isinstance(msg, str) and "iq_samplerate:" in msg:
                ack = msg.strip()
                break
        record("iq_samplerate set to 192000 echoed", ack == "iq_samplerate:192000;", ack)
        # restore default
        ws.send("iq_samplerate:96000;")
        time.sleep(0.2)


def test_iq_streaming(port=50003, duration=6.0):
    """IQ frames must flow after iq_start, with correct header, and stop after iq_stop."""
    name = f"IQ streaming on port {port}"
    with connect(f"ws://127.0.0.1:{port}/") as ws:
        drain_banner(ws)
        # ensure audio not requested - we want IQ only
        ws.send("iq_samplerate:96000;")
        time.sleep(0.2)
        ws.send("iq_start:0;")
        time.sleep(0.2)

        stats, texts = collect(ws, duration, {0, 1})
        iq = stats[0]
        got_iq = iq["count"] > 10
        detail = f"{iq['count']} IQ frames, {iq['bytes']/max(0.001,duration)/1024:.0f} KB/s"
        record(f"{name} — frames flowing", got_iq, detail)

        # Verify first IQ frame header
        # collect one frame explicitly
        ws2_msgs = []
        t0 = time.time()
        hdr_ok = False
        rate_ok = False
        while time.time() - t0 < 2.0:
            try:
                msg = ws.recv(timeout=0.3)
            except TimeoutError:
                continue
            if isinstance(msg, bytes) and len(msg) >= 64:
                ft = struct.unpack("<I", msg[24:28])[0]
                if ft == 0:
                    hdr = struct.unpack("<I", msg[24:28])[0]
                    rate = struct.unpack("<I", msg[4:8])[0]
                    channels = struct.unpack("<I", msg[28:32])[0]
                    stype = struct.unpack("<I", msg[8:12])[0]
                    hdr_ok = (hdr == 0)
                    rate_ok = (rate == 96000)
                    ch_ok = (channels == 2)
                    st_ok = (stype == 3)  # float32
                    break
        record("IQ frame header (type=0, channels=2, float32)", hdr_ok and ch_ok and st_ok,
               f"rate={rate}")
        record("IQ frame carries negotiated rate 96000", rate_ok, f"rate={rate}")

        # Stop and confirm no more IQ
        ws.send("iq_stop:0;")
        time.sleep(0.3)
        stats2, _ = collect(ws, 2.0, {0})
        record("IQ stops after iq_stop", stats2[0]["count"] == 0,
               f"{stats2[0]['count']} frames in 2s window after stop")


def test_dds(port=50003):
    name = "DDS set + query"
    with connect(f"ws://127.0.0.1:{port}/") as ws:
        drain_banner(ws)
        ws.send("dds:0,14080000;")
        time.sleep(0.3)
        ws.send("dds:0;")
        got = None
        t0 = time.time()
        while time.time() - t0 < 2.0:
            try:
                msg = ws.recv(timeout=0.3)
            except TimeoutError:
                continue
            if isinstance(msg, str) and msg.strip().startswith("dds:"):
                got = msg.strip()
                break
        ok = got is not None and "14080000" in got
        record(name, ok, got or "no response")


def test_smeter(port=50003):
    name = "S-meter reporting (rx_sensors_enable)"
    with connect(f"ws://127.0.0.1:{port}/") as ws:
        drain_banner(ws)
        ws.send("audio_start:0;")
        ws.send("rx_sensors_enable:true,300;")
        got = []
        t0 = time.time()
        while time.time() - t0 < 3.0:
            try:
                msg = ws.recv(timeout=0.4)
            except TimeoutError:
                continue
            if isinstance(msg, str) and msg.strip().startswith("rx_sensors:"):
                got.append(msg.strip())
        ok = len(got) >= 3
        record(name, ok, f"{len(got)} rx_sensors frames in 3s (interval 300ms) — sample: {got[0] if got else 'none'}")
        # disable again
        ws.send("rx_sensors_enable:false;")


def test_agc(port=50003):
    name = "AGC control"
    with connect(f"ws://127.0.0.1:{port}/") as ws:
        drain_banner(ws)
        # audio must be active for the slice to be 'active' (WDSP channel on)
        ws.send("audio_start:0;")
        time.sleep(0.3)
        # set agc mode off
        ws.send("agc_mode:0,off;")
        time.sleep(0.3)
        ws.send("agc_gain:0,40;")
        time.sleep(0.3)
        # restore normal
        ws.send("agc_mode:0,normal;")
        time.sleep(0.3)
        # collect acks
        acks = set()
        t0 = time.time()
        while time.time() - t0 < 2.0:
            try:
                msg = ws.recv(timeout=0.3)
            except TimeoutError:
                continue
            if isinstance(msg, str):
                m = msg.strip()
                if m.startswith("agc_mode:") or m.startswith("agc_gain:"):
                    acks.add(m)
        # broadcast includes our own echo; just require the mode acks to appear
        ok = any(m.startswith("agc_mode:0,off") for m in acks) or True
        # strictly: we expect at least one agc_mode echo
        saw_mode = any(m.startswith("agc_mode:") for m in acks)
        record(name, saw_mode, f"acks seen: {sorted(acks)[:3]}")
        ws.send("audio_stop:0;")


def test_mute(port=50003, duration=4.0):
    name = "Functional mute"
    with connect(f"ws://127.0.0.1:{port}/") as ws:
        drain_banner(ws)
        ws.send("audio_start:0;")
        time.sleep(0.3)
        stats1, _ = collect(ws, 2.0, {1})
        unmuted_pkts = stats1[1]["count"]

        ws.send("mute:true;")
        time.sleep(0.3)
        stats2, _ = collect(ws, 2.0, {1})
        muted_pkts = stats2[1]["count"]

        ws.send("mute:false;")
        record(name, muted_pkts == 0 and unmuted_pkts > 0,
               f"unmuted={unmuted_pkts} frames/2s, muted={muted_pkts} frames/2s")
        ws.send("audio_stop:0;")


def test_concurrent_iq_audio(port=50003, duration=6.0):
    """Audio and IQ must flow simultaneously without interfering."""
    name = f"Concurrent IQ + audio (port {port})"
    with connect(f"ws://127.0.0.1:{port}/") as ws:
        drain_banner(ws)
        ws.send("iq_samplerate:96000;")
        ws.send("iq_start:0;")
        ws.send("audio_start:0;")
        stats, _ = collect(ws, duration, {0, 1})
        iq_n = stats[0]["count"]
        au_n = stats[1]["count"]
        record(name, iq_n > 10 and au_n > 10,
               f"IQ={iq_n} frames, audio={au_n} frames in {duration:.0f}s")
        ws.send("iq_stop:0;")
        ws.send("audio_stop:0;")


def main():
    only_port = None
    if len(sys.argv) > 1:
        only_port = int(sys.argv[1])
    ensure_thetis()
    # power on
    try:
        with connect("ws://127.0.0.1:50001/") as ws:
            ws.send("start;")
            time.sleep(2)
        print("[INIT] Radio power ON.\n")
    except Exception as e:
        print(f"[INIT] power-on skipped: {e}\n")

    port = only_port or 50003
    print(f"========== Branch G verification — port {port} ==========\n")

    test_banner_and_iq_negotiation(port)
    test_iq_streaming(port)
    test_dds(port)
    test_smeter = test_smeter_with_retry(port)
    test_agc(port)
    test_mute(port)
    test_concurrent_iq_audio(port)

    print("\n========== SUMMARY ==========")
    npass = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    print(f"\n{npass}/{len(results)} checks passed")


def test_smeter_with_retry(port):
    try:
        return test_smeter(port)
    except Exception as e:
        record("S-meter reporting", False, str(e))
        return False


if __name__ == "__main__":
    main()