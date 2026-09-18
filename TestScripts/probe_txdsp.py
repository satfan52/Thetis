#!/usr/bin/env python3
"""Read-only check of the new TX microphone/processor TCI commands (no TX).

Queries mic_gain / tx_comp / tx_dexp / vox, then sets one value and confirms the
console echoes it back, and restores the original.

  python probe_txdsp.py [port]
"""
import asyncio
import sys

import websockets

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 50001
KEYS = ("mic_gain", "tx_comp", "tx_dexp", "vox")


async def main():
    async with websockets.connect(f"ws://127.0.0.1:{PORT}", ping_interval=None) as ws:
        await ws.send("protocol:1;")
        await ws.send("start;")
        await asyncio.sleep(0.8)

        seen = {}

        async def pump(sec):
            t0 = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - t0 < sec:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.4)
                except asyncio.TimeoutError:
                    continue
                if isinstance(msg, bytes):
                    continue
                for fr in msg.split(";"):
                    f = fr.strip()
                    k = f.split(":")[0].lower()
                    if k in KEYS:
                        seen[k] = f

        await pump(2.0)
        print("--- state pushed on connect ---")
        for k in KEYS:
            print(f"   {seen.get(k, '(none)')}")
        before = dict(seen)

        print("--- explicit queries ---")
        for k in KEYS:
            await ws.send(f"{k}:0;")
        await pump(1.5)
        for k in KEYS:
            print(f"   {seen.get(k)}")

        # set: compander to 5 dB, then back to what it was
        print("--- set tx_comp to 5, then restore ---")
        await ws.send("tx_comp:0,5;")
        await pump(1.5)
        print("   after set :", seen.get("tx_comp"))
        try:
            orig = int(before.get("tx_comp", "tx_comp:0,0,false").split(",")[1])
        except Exception:
            orig = 0
        await ws.send(f"tx_comp:0,{orig};")
        await pump(1.5)
        print("   restored  :", seen.get("tx_comp"))

        # set: microphone gain one step down, then restore (no TX involved)
        try:
            mic_db = int(before["mic_gain"].split(",")[1])
        except Exception:
            mic_db = None
        if mic_db is not None:
            print(f"--- set mic_gain to {mic_db - 2}, then restore ---")
            await ws.send(f"mic_gain:0,{mic_db - 2};")
            await pump(1.5)
            print("   after set :", seen.get("mic_gain"))
            await ws.send(f"mic_gain:0,{mic_db};")
            await pump(1.5)
            print("   restored  :", seen.get("mic_gain"))

        await ws.send("stop;")


asyncio.run(main())