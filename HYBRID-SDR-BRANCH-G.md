# Hybrid SDR Branch G — SDR Software Compatibility for Headless TCI Ports (RX3–RX8)

Branch G extends Branch F with the **SDR-software compatibility feature set** for the headless TCI ports. The goal is not full parity with the original TCI server on port 50001 — it is the minimal set of capabilities that real SDR software actually needs: CW Skimmer, Skimmer Server, panadapter/spectrum clients, and monitoring dashboards.

---

## 1. What Was Added (Branch G)

### 1.1 IQ Streaming (Tier 1 — required by CW Skimmer and panadapters)

The largest capability gap in Branch F was the absence of an IQ stream. Without it, CW Skimmer, Skimmer Server, SDR Console panadapters and any spectrum-based tool could not use the headless ports. Branch G closes that gap:

- **Commands**: `iq_start:<trx>;`, `iq_stop:<trx>;`, `iq_samplerate:<rate>;` (all functional, per-client).
- **Data path**: `cmaster.OnTCIRxIQOutSamples` already fires for all 8 receivers; Branch G adds a `HeadlessIQPublisher` delegate and a `HeadlessIQWantsIQ` gate predicate. When any headless client requests IQ, a copy of the native IQ buffer is forwarded to the owning headless server and streamed to that client.
- **Works with or without port 50001**: IQ forwarding no longer requires the original TCI server to be running.
- **Sample rate**: negotiated per client via `iq_samplerate` and snapped to 48k / 96k / 192k / 384 kHz. **Default is 96 kHz (~0.77 MB/s per streaming receiver)**; 96 kHz covers the ~±48 kHz range that CW Skimmer needs for typical CW segments.
- **Resampling**: linear interpolation from the native 192 kHz hardware rate when the client negotiates a lower rate.
- **Frame format**: standard TCI binary frame, `frame_type = 0` (IQ_STREAM), float32 interleaved I/Q, 64-byte header — byte-compatible with the original server's IQ frames.

### 1.2 DDS — Panadapter Center Frequency (Tier 1)

`dds:0,<hz>;` sets the slice's center frequency (same path as `vfo`). CW Skimmer uses `dds` to learn which frequency range the incoming IQ stream covers. Implemented together with IQ streaming; a query form is also supported.

### 1.3 S-Meter / Sensors (Tier 1)

`rx_sensors_enable:<true/false>[,<interval_ms>];` enables periodic signal-level reporting:

- Level is computed from the **streaming audio RMS** (no dependency on the console meter system, which only covers RX1/RX2).
- Reported as `rx_sensors:0,<dBFS>;` and `rx_channel_sensors:0,0,<dBFS>,<avg>,<peak>;`
- Interval configurable 100–2000 ms (default 500 ms). Accumulation happens inside the existing audio packet loop — zero extra DSP work, negligible CPU.
- Clamp: -160 dBFS … 0 dBFS (0 dBFS = digital full scale).

### 1.4 AGC Control (Tier 2)

CW Skimmer performs best with AGC off or FAST; weak-signal digimode users want AGC off to avoid noise pumping between FT8 slots. Branch G makes the previously hardcoded AGC client-controllable:

| Command | Effect |
| :--- | :--- |
| `agc_mode:0,<off/long/slow/normal/fast/custom>;` | Sets WDSP AGC mode on the slice channel |
| `agc_auto_ex:0,<true/false>;` | `false` switches to FIXD (manual gain); `true` restores MED |
| `agc_gain:0,<db>;` | Manual fixed gain when AGC is off (-20 … +120 dB), via `WDSP.SetRXAAGCFixed` |

The **defaults are unchanged** (MED mode, 90 dB top) — a client that never sends these commands sees exactly the Branch F behavior.

### 1.5 Functional Mute (Tier 2)

`mute:true;` / `mute:false;` now actually silences/restores this client's RX audio stream (previously a stub that always echoed `false`).

---

## 2. What Deliberately Was NOT Added

Per the compatibility analysis, the following full-server features are **not** needed by SDR software and were left out:

| Feature | Reason |
| :--- | :--- |
| Split / RIT / XIT / VFO swap / lock | Skimmers and digimode apps never send them; the echo stubs are already compatible |
| CW keyer / macros | Skimmer is receive-only; digimodes key via `trx` + TX audio |
| Line out (VAC) | VAC1/VAC2 are global shared resources with dedicated roles; headless TX audio bypasses VAC entirely |
| Spots (write side) | Spot reporting flows through port 50001 / the spot pipeline, not per-slice |
| TX profiles, TX sensors | No SDR client uses them |
| `shutdown_ex`, attenuator/preamp `_ex` commands | Dangerous or shared-hardware; not SDR-relevant |
| Second TRX per port | Breaks the 1-port-1-receiver design SDR clients expect |

---

## 3. Performance Notes

- **IQ bandwidth**: at the default 96 kHz cap, one streaming receiver produces ~0.77 MB/s. All seven headless ports streaming IQ simultaneously ≈ 5.4 MB/s — trivial on LAN, not suitable for WAN.
- **CPU**: the IQ data already exists in the native pipeline (the callback fires regardless); the added cost is one buffer copy + frame send per active IQ client, gated by `HeadlessIQWantsIQ` so idle receivers do zero extra work.
- **S-meter**: computed during the existing audio packet loop; no additional DSP threads.
- **No IQ resampler allocations on the native path**: the copy is a single `Buffer.BlockCopy` per block, performed only when a client has requested IQ.

---

## 4. Testing Status

- The solution compiles cleanly (`dotnet msbuild`, Release x64, 0 errors).
- **Hardware verification against the Red Pitaya has NOT yet been performed for Branch G.**

An automated verification harness is ready in `TestScripts/test_branch_g.py`. It checks, per headless port:

1. Banner + `iq_samplerate` negotiation (default 96000, set/echo round-trip)
2. IQ streaming — frames flow after `iq_start`, frame header validity (type 0, 2 channels, float32, negotiated rate), frames cease after `iq_stop`
3. DDS set + query round-trip
4. S-meter — `rx_sensors_enable` yields periodic `rx_sensors` frames
5. AGC — `agc_mode` / `agc_gain` command echoes
6. Mute — audio frames stop while muted, resume after unmute
7. Concurrent IQ + audio streaming without interference

Run with:

```
python3 TestScripts\test_branch_g.py [port]
```

(Requires Thetis Branch G running with the Red Pitaya on the network, and the `websockets` Python package — the same harness family as `test_all_channels_audio.py`.)

Additionally, run **CW Skimmer** against a headless port (e.g. 50003) to verify the full IQ consumption path end-to-end, and **WSJT-X** to confirm the Branch F TX path is unaffected.

---

## 5. Source Changes

| File | Change |
| :--- | :--- |
| `Project Files/Source/Console/cmaster.cs` | `HeadlessIQPublisher`, `HeadlessIQWantsIQ` delegates; IQ forwarding in `OnTCIRxIQOutSamples` |
| `Project Files/Source/Console/HeadlessTciServer.cs` | IQ publish path (manager → server → client), `BuildIQPayload`, `iq_start`/`iq_stop`/`iq_samplerate`/`dds`/`rx_sensors_enable`/`agc_*`/`mute` handlers, S-meter accumulation |
| `Project Files/Source/Console/Thetis.csproj` | netstandard facade reference (build environment) |
| `Project Files/Source/Midi2Cat/Midi2Cat.csproj` | reference-assemblies import (build environment) |
| `Project Files/Source/RawInput/RawInput.csproj` | reference-assemblies import (build environment) |

---

## 6. Branch Lineage

```
master
  └── feature/dedicated-processed-tx-output (Branch D)
        └── E (CI-V + Processed TX Output)
              └── F (Multi-Port Headless TCI Server)
                    └── G (SDR software compatibility: IQ, DDS, S-meter, AGC, mute)
```
