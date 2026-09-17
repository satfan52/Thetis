#!/usr/bin/env python3
"""Read-only TCI sniffer: connect to Thetis :50001, log every frame for N seconds."""
import socket, sys, time, re

HOST, PORT = "127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 50001
DUR = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
PROBE = sys.argv[3] if len(sys.argv) > 3 else None   # e.g. "vfo:0,0,14074000"

s = socket.create_connection((HOST, PORT), timeout=5)
s.settimeout(0.3)
send = lambda t: s.sendall((t + ";").encode())

send("protocol:1")
send("start")
send("rx_enable:0,true")

if PROBE:
    time.sleep(1.0)
    print(">>> PROBE", PROBE, flush=True)
    send(PROBE)

buf = ""
t0 = time.time()
seen = {}
order = []
while time.time() - t0 < DUR:
    try:
        d = s.recv(65536)
        if not d:
            break
    except socket.timeout:
        continue
    buf += d.decode("utf-8", "replace")
    while ";" in buf:
        msg, buf = buf.split(";", 1)
        if not msg:
            continue
        k = msg.split(":", 1)[0]
        if k in ("iq_start", "iq_stop", "iq_samplerate", "audio_start", "audio_stop"):
            seen.setdefault(k, 0)
            seen[k] += 1
            continue
        if k in ("vfo", "dds", "if", "txfreq"):
            seen.setdefault(k, 0)
            seen[k] += 1
            print(f"[{time.time()-t0:6.2f}s] {msg}", flush=True)
        else:
            if k not in seen:
                order.append(k)
            seen[k] = seen.get(k, 0) + 1

print("--- counts ---")
for k in order:
    print(f"{k:28s} {seen[k]}")
s.close()
