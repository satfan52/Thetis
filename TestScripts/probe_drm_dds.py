#!/usr/bin/env python3
"""Measure the DRM DDS offset as the SERVER reports it.

Sets a known dial frequency, switches to DRM, and prints the dds/vfo echoes so
we can see whether the IQ stream is centred on the dial or 12 kHz below it.

  python probe_drm_dds.py [port]
"""
import asyncio
import sys

import websockets

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 50001
DIAL = 15_750_000


async def main():
    async with websockets.connect(f"ws://127.0.0.1:{PORT}", ping_interval=None) as ws:
        await ws.send("protocol:1;")
        await ws.send("start;")
        await asyncio.sleep(0.8)
        try:
            while True:
                await asyncio.wait_for(ws.recv(), timeout=0.3)
        except asyncio.TimeoutError:
            pass

        async def drain(sec, keys=("dds", "vfo", "modulation", "ctun")):
            seen = {}
            t0 = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - t0 < sec:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.4)
                except asyncio.TimeoutError:
                    continue
                for fr in msg.split(";"):
                    f = fr.strip()
                    for k in keys:
                        if f.lower().startswith(k):
                            seen[k] = f
            return seen

        await ws.send(f"vfo:0,0,{DIAL};")
        await asyncio.sleep(0.6)
        for tok in ("USB", "DRM"):
            await ws.send(f"modulation:0,{tok};")
            await asyncio.sleep(0.8)
            await ws.send(f"vfo:0,0,{DIAL};")
            await asyncio.sleep(0.6)
            print(f"--- {tok} ---", flush=True)
            for k, v in (await drain(1.5)).items():
                print(f"    {k}: {v}")
        await ws.send("modulation:0,USB;")
        await ws.send("stop;")


asyncio.run(main())
