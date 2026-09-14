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

- The solution compiles cleanly (`dotnet msbuild`, Release x64, 0 errors). **Only the managed code is rebuilt on the development PC** (no MSVC toolchain there): `ChannelMaster.dll` / `wdsp.dll` are the Branch F build (`ea84d7f`). Branch G makes **no native change** (the ChannelMaster sources are identical to Branch F).
- **TX path hardware-VERIFIED (2026-09-13)**: WSJT-X on a headless port transmitted against the live Red Pitaya + IC-7100 station — TX_CHRONO pacing, TX_AUDIO_STREAM ingestion, WDSP TX engagement, CI-V steering, IC-7100 keying/audio.
- **MiniTCI TX hardware-VERIFIED (2026-09-14)**: PTT (mic) and TUNE from MiniTCI on RX3 produce clean audio and RF on the IC-7100 **with VAC1 disabled in Thetis** — the intended, VAC-independent operation. Verified by the operator on the IC-7100 monitor and power meter.
- **Verified live with MiniTCI**: IQ streaming/panafall, DDS, S-meter, AGC modes, mute, VFO-centred display, Quisk mouse model, MOX broadcast (`mox:0,…`) muting the monitor.
- **Still open**: CW Skimmer against a headless port (IQ consumption by a third-party client). Harness: `TestScripts/test_branch_g.py [port]`.

### 4.1 Station notes learned during verification

| Topic | Finding |
| :--- | :--- |
| Firmware | The Red Pitaya runs Pavel Demin's *receiver* HPSDR firmware (8 DDCs, no transmitter). It speaks Protocol 1; every frame carries mic-sample slots, so the Thetis TX DSP stream is always clocked (`networkproto1.c:416`). No MOX and no extra clock are needed for TCI TX — a MOX assert tried during development was **removed** (it would put the VAC1 mic on air). |
| TX Out driver | WASAPI works on **Setup → Audio → TX Output** with a **2048-sample** buffer (512 gives distorted/underrunning audio on this USB codec). MME works at 512. Both are valid; WASAPI/2048 is in use. VAC1 runs WASAPI. |
| TX-chain probe | While a headless client transmits, Thetis sends it `probe:q=…,calls=…,samps=…,mic=…,alc=…,pwr=…,vac1=…,txout_under=…,txout_over=…,txout_fill=…;` once per second (queued TCI samples, pulls into the TX DSP, WDSP MIC_PK/ALC_PK/PWR meters, VAC1 state, TX-Out IVAC ring diagnostics). MiniTCI prints these lines in its log. Use them to locate a TX-audio fault stage by stage. |
| Slice AF gain | Headless slice default AF gain is 0.5 (was 0.05) so client audio level matches Thetis. |
| RX AGC during TX | The slice AGC is snapped to FIXED during a headless TX and restored on release, so RX audio is at full level immediately after PTT (no multi-second AGC recovery). |

### 4.2 MiniTCI client (`TestScripts/MiniTCI.py`, `C:\Thetis\MiniTCI.exe`)

Stand-alone tkinter/`sounddevice` client with its **own audio devices** — it does not use VAC1 and can run on another PC. Features: Thetis-style VFO-centred panafall with Quisk mouse model and Y-zero / Y-scale / zoom / waterfall-intensity sliders; band/mode/AGC controls; direct-frequency entry; S-meter (RX signal, mic or Tune level while transmitting); PTT with monitor mute and a configurable TX tail (ms, default 350); TUNE (1500 Hz tone, configurable drive, toggles on/off); settings persisted in `settings.json`; timestamped log with the Thetis TX probe.

## 5. Source Changes

| File | Change |
| :--- | :--- |
| `Project Files/Source/Console/cmaster.cs` | `HeadlessIQPublisher`, `HeadlessIQWantsIQ` delegates; IQ forwarding in `OnTCIRxIQOutSamples` |
| `Project Files/Source/Console/HeadlessTciServer.cs` | IQ publish path (manager → server → client), `BuildIQPayload`, `iq_start`/`iq_stop`/`iq_samplerate`/`dds`/`rx_sensors_enable`/`agc_*`/`mute` handlers, S-meter accumulation |
| `Project Files/Source/Console/TxArbiter.cs` | Digital TX request/release: engage WDSP TX channel, switch `SetTXTCIAudioRun`, CI-V steer + key, LIFO preemption; voice always has priority; **no MOX assert** |
| `Project Files/Source/Console/CAT/CIVController.cs` | 80 ms settling between the last CI-V frequency frame and PTT (prevents TX on the previous VFO) |
| `Project Files/Source/Console/HeadlessSliceManager.cs` | default slice AF gain 0.5 |
| `Project Files/Source/Console/cmaster.cs` | IQ swap gate `tciServer == null \|\| tciServer.IQSwap` (headless spectrum was mirrored); once-per-second TX probe in `serviceTCITxProtocol` |
| `Project Files/Source/Console/HeadlessTciServer.cs` | `trx`/`tune` handlers freeze/restore slice AGC; `mox:0,true/false;` broadcast via `Console.MoxChangeHandlers`; `ReportProbe` |
| `Project Files/Source/Console/console.cs` | `MAX_TONE_MAG` 0.99999 → 0.2 (Thetis TUNE tone at −14 dBFS instead of full scale — the IC-7100 DATA input clipped); 8-DDC rate table; headless port listing |
| `TestScripts/MiniTCI.py` | the client (see 4.2); built with PyInstaller to `TestScripts/dist/MiniTCI.exe` |
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
