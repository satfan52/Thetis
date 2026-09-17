#!/usr/bin/env python3
"""Verify TCI mode selection end-to-end: send modulation:0,<tok> and report the
console's echo. Read-only - no TX.  Usage: python probe_modulation.py [port]"""
import socket, sys, time

HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 50001

s = socket.create_connection((HOST, PORT), timeout=5)
s.settimeout(0.3)
send = lambda t: s.sendall((t + ";").encode())

send("protocol:1")
send("start")
time.sleep(1.0)

buf = ""
echoes = []


def pump(sec):
    global buf
    t0 = time.time()
    while time.time() - t0 < sec:
        try:
            d = s.recv(65536)
            if not d:
                break
            buf += d.decode(errors="replace")
        except socket.timeout:
            continue
    while ";" in buf:
        frame, buf = buf.split(";", 1)
        f = frame.strip()
        if f.lower().startswith("modulation"):
            echoes.append(f)


pump(0.5)
for tok in ("DRM", "SPEC", "USB"):
    before = len(echoes)
    print(f">>> send modulation:0,{tok}", flush=True)
    send(f"modulation:0,{tok}")
    pump(2.5)
    new = echoes[before:]
    print(f"    echoes: {new}", flush=True)

print("\nfinal echo seen:", echoes[-1] if echoes else "NONE")
s.close()
