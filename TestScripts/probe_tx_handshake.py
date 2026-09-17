#!/usr/bin/env python3
"""AUTHORISED short key test (2 s) that reports what the SERVER does with a
'tci'-marked key request on each port: the raw trx echo (does it carry the tci
marker?) and whether the server asks for TX audio (chrono frames) at all.

  python probe_tx_handshake.py [port] [seconds]
"""
import asyncio
import struct
import sys

import websockets

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 50001
SECS = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
BURST = len(sys.argv) > 3 and sys.argv[3] == "burst"

# MiniTCI's connect burst, to bisect what makes the server stop asking for audio
APP_BURST = [
    "iq_samplerate:96000", "iq_start:0", "audio_start:0",
    "rx_sensors_enable:true,250", "vfo:0,0,7097917", "modulation:0,LSB",
    "rx_filter_band:0,-2800,-100", "rx_ctun_ex:0,false",
    "rx_channel_enable:0,1,true", "vfoasub:0,7077020", "subrx_state:0",
    "subrx:0,true", "vfo:1,0,7077020", "sub_mode:0,LSB",
    "sub_filter:0,-1800,-100", "split_enable:0,true", "rx_balance:0,0.50",
    "agc_mode:0,normal", "agc_gain:0,81",
]

TX_CHRONO = 3


async def main():
    chronos = 0
    trx_frames = []
    async with websockets.connect(f"ws://127.0.0.1:{PORT}", ping_interval=None) as ws:
        await ws.send("protocol:1;")
        await ws.send("start;")
        await ws.send("tx_stream_audio_buffering:100;")
        await ws.send("audio_stream_sample_type:float32;")
        await ws.send("audio_stream_channels:1;")
        await ws.send("audio_stream_samples:1024;")
        if BURST:
            for cmd in APP_BURST:
                await ws.send(cmd + ";")
                await asyncio.sleep(0.05)
        await asyncio.sleep(0.4)
        try:
            while True:
                await asyncio.wait_for(ws.recv(), timeout=0.2)
        except asyncio.TimeoutError:
            pass

        print(f">>> trx:0,true,tci  (port {PORT}, {SECS}s, burst={BURST})", flush=True)
        await ws.send("trx:0,true,tci;")

        t0 = asyncio.get_event_loop().time()
        sent_audio = 0
        try:
            while asyncio.get_event_loop().time() - t0 < SECS:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                if isinstance(msg, bytes):
                    if len(msg) >= 28:
                        ftype = struct.unpack("<I", msg[24:28])[0]
                        if ftype == TX_CHRONO:
                            chronos += 1
                            length = struct.unpack("<i", msg[20:24])[0]
                            n = max(64, min(length, 4096) * 4)
                            hdr = struct.pack("<16I", 0, 48000, 3, 0, 0,
                                              length, 2, 1, 0, 0, 0, 0, 0, 0, 0, 0)
                            await ws.send(hdr + b"\x00" * min(length * 4, 8192))
                            sent_audio += 1
                else:
                    for fr in msg.split(";"):
                        if fr.strip().lower().startswith("trx"):
                            trx_frames.append(fr.strip())
        finally:
            await ws.send("trx:0,false,tci;")
            await asyncio.sleep(0.5)
            try:
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.3)
                    if not isinstance(msg, bytes):
                        for fr in msg.split(";"):
                            if fr.strip().lower().startswith("trx"):
                                trx_frames.append(fr.strip())
            except asyncio.TimeoutError:
                pass
            await ws.send("stop;")

    print("trx frames seen:", trx_frames)
    print("TX chrono requests:", chronos)
    print("TX audio frames sent:", sent_audio)
    marked = any(f.lower().endswith("tci") for f in trx_frames)
    print("\nserver ACCEPTED the tci audio marker:", marked)
    print("server DREW TX audio from the client:", chronos > 0)


asyncio.run(main())