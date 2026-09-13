import sys
import time
import socket
import struct
import threading
import subprocess
from websockets.sync.client import connect

sys.stdout.reconfigure(line_buffering=True)

def is_port_listening(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(0.5)
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except:
        return False

def ensure_thetis():
    if is_port_listening(50001):
        print("[INIT] Thetis is already running.")
        return None

    print("[INIT] Launching Thetis.exe...")
    proc = subprocess.Popen(["C:\\Thetis\\Thetis.exe"], cwd="C:\\Thetis")
    for sec in range(1, 20):
        time.sleep(1)
        if is_port_listening(50001):
            print(f"[INIT] Thetis port 50001 ready at second {sec} (PID {proc.pid})")
            time.sleep(2)
            return proc
    raise RuntimeError("Thetis failed to start.")

def turn_on_radio():
    print("[INIT] Turning on radio power via port 50001...")
    with connect("ws://127.0.0.1:50001/") as ws:
        ws.send("start;")
        time.sleep(2)

def stream_worker(port, duration, results_dict):
    uri = f"ws://127.0.0.1:{port}/"
    with connect(uri) as ws:
        # Drain
        while True:
            try:
                msg = ws.recv(timeout=0.2)
            except TimeoutError:
                break

        ws.send("audio_start:0;")

        packets = 0
        total_bytes = 0
        dropouts = 0
        intervals = []
        last_time = None
        t_start = time.time()

        while time.time() - t_start < duration:
            try:
                msg = ws.recv(timeout=1.0)
                now = time.time()
                if isinstance(msg, bytes):
                    packets += 1
                    total_bytes += len(msg)
                    if last_time is not None:
                        dt = (now - last_time) * 1000.0
                        intervals.append(dt)
                        if dt > 150.0:
                            dropouts += 1
                    last_time = now
            except TimeoutError:
                dropouts += 1

        ws.send("audio_stop:0;")
        elapsed = time.time() - t_start

    kb_s = total_bytes / elapsed / 1024.0 if elapsed > 0 else 0
    avg_dt = sum(intervals) / len(intervals) if intervals else 0
    min_dt = min(intervals) if intervals else 0
    max_dt = max(intervals) if intervals else 0
    passed = (packets > 0) and (dropouts == 0)

    results_dict[port] = {
        "port": port,
        "packets": packets,
        "bytes": total_bytes,
        "kb_s": kb_s,
        "min_dt": min_dt,
        "avg_dt": avg_dt,
        "max_dt": max_dt,
        "dropouts": dropouts,
        "passed": passed
    }

def main():
    try:
        subprocess.run(["C:\\Users\\peter\\AppData\\Local\\Python\\bin\\python.exe",
                        "C:\\Users\\peter\\.gemini\\antigravity\\brain\\9d7f8ce8-0e52-4643-bcc3-b5bd4f8b982d\\scratch\\reset_radio.py"],
                       check=True, capture_output=True)
    except:
        pass

    thetis_proc = ensure_thetis()
    try:
        turn_on_radio()

        duration = 10
        print(f"\n>>> Running CONCURRENT audio test on ports 50001 (RX1) and 50008 (RX8) for {duration}s...")
        results = {}
        t1 = threading.Thread(target=stream_worker, args=(50001, duration, results))
        t8 = threading.Thread(target=stream_worker, args=(50008, duration, results))

        t1.start()
        t8.start()

        t1.join()
        t8.join()

        print("\n" + "=" * 75)
        print(f"{'PORT':<8} {'PACKETS':<10} {'RATE':<12} {'INTERVALS (min/avg/max)':<25} {'DROPOUTS':<10} {'STATUS'}")
        print("=" * 75)
        all_passed = True
        for p in [50001, 50008]:
            r = results[p]
            if not r["passed"]: all_passed = False
            intervals_str = f"{r['min_dt']:.1f}/{r['avg_dt']:.1f}/{r['max_dt']:.1f} ms"
            status_str = "PASSED" if r["passed"] else "FAILED"
            print(f"{r['port']:<8} {r['packets']:<10} {r['kb_s']:.1f} KB/s    {intervals_str:<25} {r['dropouts']:<10} {status_str}")
        print("=" * 75)

        if all_passed:
            print("[SUCCESS] CONCURRENT STREAMING ON 50001 & 50008 PASSED WITH 0 DROPOUTS!")
        else:
            print("[FAILURE] Concurrent streaming failed.")

    finally:
        if thetis_proc:
            print("[CLEANUP] Stopping Thetis process...")
            thetis_proc.terminate()
            try:
                thetis_proc.wait(timeout=5)
            except:
                thetis_proc.kill()

if __name__ == "__main__":
    main()
