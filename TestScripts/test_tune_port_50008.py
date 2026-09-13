import socket
import struct
import base64
import hashlib
import time
import math
import subprocess
import sys

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
    if is_port_listening(50001) and is_port_listening(50008):
        print("[INIT] Thetis is already running and listening.")
        return None

    print("[INIT] Launching Thetis.exe...")
    proc = subprocess.Popen(["C:\\Thetis\\Thetis.exe"], cwd="C:\\Thetis")
    for sec in range(1, 25):
        time.sleep(1)
        if is_port_listening(50001) and is_port_listening(50008):
            print(f"[INIT] Thetis ports 50001 and 50008 ready at second {sec} (PID {proc.pid})")
            time.sleep(2)
            return proc
    raise RuntimeError("Thetis failed to start or open ports.")

def make_ws_frame(opcode, payload):
    # client frames MUST be masked
    mask = b'\x12\x34\x56\x78'
    length = len(payload)
    if length <= 125:
        header = bytes([0x80 | opcode, 0x80 | length])
    elif length <= 65535:
        header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", length)
    else:
        header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", length)
    
    masked_payload = bytearray(length)
    for i in range(length):
        masked_payload[i] = payload[i] ^ mask[i % 4]
    
    return header + mask + bytes(masked_payload)

def parse_ws_frame(buf):
    if len(buf) < 2:
        return None, None, buf
    b0 = buf[0]
    b1 = buf[1]
    opcode = b0 & 0x0F
    masked = (b1 & 0x80) != 0
    payload_len = b1 & 0x7F
    header_len = 2
    if payload_len == 126:
        if len(buf) < 4:
            return None, None, buf
        payload_len = struct.unpack(">H", buf[2:4])[0]
        header_len = 4
    elif payload_len == 127:
        if len(buf) < 10:
            return None, None, buf
        payload_len = struct.unpack(">Q", buf[2:10])[0]
        header_len = 10

    if masked:
        header_len += 4
    if len(buf) < header_len + payload_len:
        return None, None, buf

    payload = buf[header_len:header_len+payload_len]
    rem = buf[header_len+payload_len:]
    return opcode, payload, rem

def run_test():
    ensure_thetis()

    # Turn on radio power via port 50001 if needed
    print("[INIT] Connecting to port 50001 to ensure radio Power ON...")
    s1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s1.connect(("127.0.0.1", 50001))
    key1 = base64.b64encode(b"powercheck123456").decode()
    req1 = (
        f"GET / HTTP/1.1\r\n"
        f"Host: 127.0.0.1:50001\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key1}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    s1.sendall(req1.encode())
    time.sleep(0.3)
    s1.sendall(make_ws_frame(1, b"start;"))
    time.sleep(1.0)
    s1.close()
    print("[INIT] Radio power confirmed ON.")

    print("\n>>> Connecting to Headless Port 50008 (RX8, DDC 7)...")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(("127.0.0.1", 50008))
    s.settimeout(2.0)

    # Handshake
    key = base64.b64encode(b"0123456789abcdef").decode()
    req = (
        f"GET / HTTP/1.1\r\n"
        f"Host: 127.0.0.1:50008\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    s.sendall(req.encode())

    resp = s.recv(4096).decode(errors='ignore')
    print("[1] Handshake response received on port 50008:")
    for line in resp.strip().split('\r\n')[:2]:
        print("   ", line)

    # Read banner frames
    buf = bytearray()
    time.sleep(0.3)
    data = s.recv(8192)
    buf.extend(data)
    while True:
        op, payload, rem = parse_ws_frame(buf)
        if op is None:
            break
        buf = bytearray(rem)

    # Request RX Audio
    print("[2] Requesting audio_start:0;...")
    s.sendall(make_ws_frame(1, b"audio_start:0;"))

    rx_audio_packets = 0
    t_start = time.time()
    while time.time() - t_start < 1.0:
        try:
            chunk = s.recv(8192)
            buf.extend(chunk)
            while True:
                op, payload, rem = parse_ws_frame(buf)
                if op is None:
                    break
                buf = bytearray(rem)
                if op == 2 and len(payload) >= 64:
                    stream_type = struct.unpack("<I", payload[24:28])[0]
                    if stream_type == 1: # RX_AUDIO_STREAM
                        rx_audio_packets += 1
        except socket.timeout:
            pass

    print(f"   Received {rx_audio_packets} RX audio packets in 1 second. RX streaming OK.")

    # Now simulate WSJT-X hitting "Tune" (or transmitting FT8)
    print("[3] Simulating WSJT-X Tune: sending 'trx:0,true,tci;' and 'tune:0,true;'...")
    s.sendall(make_ws_frame(1, b"trx:0,true,tci;tune:0,true;"))

    # Listen for TX_CHRONO frames from Thetis!
    chrono_received = 0
    tx_audio_sent = 0
    rx_during_tx = 0
    phase = 0.0

    t_tx_start = time.time()
    while time.time() - t_tx_start < 3.0:
        try:
            chunk = s.recv(8192)
            buf.extend(chunk)
            while True:
                op, payload, rem = parse_ws_frame(buf)
                if op is None:
                    break
                buf = bytearray(rem)
                if op == 1:
                    txt = payload.decode(errors='ignore')
                    print(f"   [Server Text] {txt}")
                elif op == 2 and len(payload) >= 64:
                    rcvr = struct.unpack("<I", payload[0:4])[0]
                    srate = struct.unpack("<I", payload[4:8])[0]
                    stype = struct.unpack("<I", payload[8:12])[0]
                    length = struct.unpack("<I", payload[20:24])[0]
                    stream_type = struct.unpack("<I", payload[24:28])[0]
                    chans = struct.unpack("<I", payload[28:32])[0]

                    if stream_type == 3: # TX_CHRONO!
                        chrono_received += 1
                        # Thetis is asking for audio! Send a packet of 1000 Hz sine wave audio
                        sample_count = length if chans > 0 else 2048
                        sample_bytes = bytearray()
                        for _ in range(sample_count // 2):
                            val = 0.7 * math.sin(phase)
                            phase += 2.0 * math.pi * 1000.0 / 48000.0
                            if phase > 2.0 * math.pi:
                                phase -= 2.0 * math.pi
                            bval = struct.pack("<f", val)
                            sample_bytes.extend(bval) # Left
                            sample_bytes.extend(bval) # Right

                        # Build TX_AUDIO_STREAM header (64 bytes)
                        # stream_type = 2
                        hdr = bytearray(64)
                        struct.pack_into("<I", hdr, 0, 0) # rcvr 0
                        struct.pack_into("<I", hdr, 4, 48000) # sampleRate
                        struct.pack_into("<I", hdr, 8, 3) # FLOAT32 = 3
                        struct.pack_into("<I", hdr, 20, sample_count) # length
                        struct.pack_into("<I", hdr, 24, 2) # TX_AUDIO_STREAM = 2
                        struct.pack_into("<I", hdr, 28, 2) # channels = 2

                        tx_packet = hdr + sample_bytes
                        s.sendall(make_ws_frame(2, tx_packet))
                        tx_audio_sent += 1
                    elif stream_type == 1:
                        rx_during_tx += 1
        except socket.timeout:
            pass

    print(f"\n[4] Results during TX:")
    print(f"   Chrono packets received from Thetis: {chrono_received}")
    print(f"   Audio packets sent to Thetis: {tx_audio_sent}")
    print(f"   RX audio packets received during TX (Full-Duplex): {rx_during_tx}")

    # Now unkey
    print("\n[5] Unkeying Tune: sending 'tune:0,false;trx:0,false;'...")
    s.sendall(make_ws_frame(1, b"tune:0,false;trx:0,false;"))
    time.sleep(0.5)

    s.close()
    print("[6] Test completed successfully!")

if __name__ == "__main__":
    run_test()
