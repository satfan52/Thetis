#!/usr/bin/env python3
"""WebSocket TCI probe: verify modulation selection reaches the console.

Sends modulation:0,<tok> on a real WebSocket TCI session and prints the
modulation echoes the server broadcasts back.  Read-only, no TX.

  python probe_modulation_ws.py [port] [tokens...]
"""
import asyncio
import sys

import websockets

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 50001
TOKENS = sys.argv[2:] or ["DRM", "SPEC", "USB"]


async def main():
    uri = f"ws://127.0.0.1:{PORT}"
    async with websockets.connect(uri, ping_interval=None) as ws:
        await ws.send("protocol:1;")
        await ws.send("start;")
        await asyncio.sleep(1.0)
        # drain the initial burst
        try:
            while True:
                await asyncio.wait_for(ws.recv(), timeout=0.3)
        except asyncio.TimeoutError:
            pass

        for tok in TOKENS:
            print(f">>> send modulation:0,{tok}", flush=True)
            await ws.send(f"modulation:0,{tok};")
            echoes, t0 = [], asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - t0 < 2.5:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.4)
                except asyncio.TimeoutError:
                    continue
                for frame in msg.split(";"):
                    f = frame.strip()
                    if f.lower().startswith("modulation"):
                        echoes.append(f)
            print(f"    echoes: {echoes}", flush=True)
        await ws.send("stop;")


asyncio.run(main())
