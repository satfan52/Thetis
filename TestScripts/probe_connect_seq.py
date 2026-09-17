#!/usr/bin/env python3
"""Replay MiniTCI's connect sequence frame by frame and report which command
makes the server close the session.  Read-only: RX streaming only, no TX.

  python probe_connect_seq.py [port]
"""
import asyncio
import sys

import websockets

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 50001
FREQ = 7_097_917

SEQ = [
    "protocol:1",
    "start",
    "iq_samplerate:96000",
    "iq_start:0",
    "audio_start:0",
    "rx_sensors_enable:true,250",
    f"vfo:0,0,{FREQ}",
    "modulation:0,LSB",
    "rx_filter_band:0,-2800,-100",
    "rx_ctun_ex:0,false",
    # sub/audio/agc state MiniTCI pushes when SUB+SPLIT are on (the live case)
    "rx_channel_enable:0,1,true",
    "vfoasub:0,7077020",
    "split_enable:0,true",
    "rx_balance:0,0.00",
    "agc_mode:0,normal",
    "agc_gain:0,81",
]


async def main():
    ws = await websockets.connect(f"ws://127.0.0.1:{PORT}", ping_interval=None)
    alive = True
    try:
        for frame in SEQ:
            try:
                await ws.send(frame + ";")
            except Exception as e:
                print(f"  [{frame}] SEND FAILED: {type(e).__name__}: {e}")
                alive = False
                break
            await asyncio.sleep(0.35)
            # liveness probe: a query must be answered
            try:
                await ws.send("modulation:0;")
                t0 = asyncio.get_event_loop().time()
                answered = False
                while asyncio.get_event_loop().time() - t0 < 4.0:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    if isinstance(msg, bytes):
                        continue          # IQ/audio stream frame
                    if msg.startswith("modulation"):
                        answered = True
                        break
                if not answered:
                    print(f"  [{frame}] NO ANSWER to liveness probe -> session dead")
                    alive = False
                    break
                print(f"  [{frame}] ok")
            except asyncio.TimeoutError:
                print(f"  [{frame}] liveness probe timed out -> session dead")
                alive = False
                break
            except websockets.ConnectionClosed as e:
                print(f"  [{frame}] CONNECTION CLOSED by server: {e}")
                alive = False
                break
    finally:
        try:
            await ws.close()
        except Exception:
            pass
    print("\nresult:", "session survived the whole sequence" if alive
          else "session died (see last frame above)")


asyncio.run(main())