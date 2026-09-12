# Hybrid SDR Branch F — Multi-Port Headless TCI Server (RX3–RX8) & Digital TX Flow Control

Branch F extends Branch E with a **multi-port headless TCI server** that exposes RX3 through RX8 on dedicated WebSocket ports (50003–50008), enabling independent digital mode applications (WSJT-X, JTDX, JS8Call, etc.) to connect directly to individual receivers without conflicting with the main operator position on port 50001.

---

## 0. Motivation & Background

### Why Branch F Was Created

The Red Pitaya SDR, running OpenHPSDR/Hermes-compatible firmware, supports **up to 8 DDC receivers** (DDC0–DDC7) over the HPSDR Protocol 2 Ethernet interface. Standard Thetis exposes only 2 of these (RX1/DDC0 and RX2/DDC1) to TCI clients on a single WebSocket port (50001), leaving receivers 3–8 inaccessible to external digital mode software.

The goal of Branch F is to make all 8 receivers simultaneously available to independent digital mode applications — each on its own dedicated TCI port — while preserving the main operator position on port 50001 with full capabilities for voice operation, panadapter display, and CI-V transceiver control.

### Use Case

A typical Branch F station configuration:

- **RX1 (port 50001)**: Main operator position — Thetis GUI with full panadapter, waterfall, DSP features, voice TX via IC-7100 CI-V, and Processed TX Output.
- **RX3 (port 50003)**: WSJT-X for FT8 on one band.
- **RX4 (port 50004)**: JTDX for FT8/FT4 on another band.
- **RX5 (port 50005)**: JS8Call for JS8 on a third band.
- **RX6 (port 50006)**: CW Skimmer or spot aggregator.
- **RX7–RX8 (ports 50007–50008)**: Additional monitoring or recording.

All 8 receivers run simultaneously on the same Red Pitaya hardware, each tuned to an independent frequency, each with its own digital mode application connected via a dedicated TCI WebSocket port.

### Hardware Compatibility

> **⚠️ Tested with Red Pitaya only.**
>
> Branch F was developed and verified exclusively against the **Red Pitaya SDR** running OpenHPSDR/Hermes-compatible firmware with 8 DDC support. The 8-receiver router pipeline in `ChannelMaster.dll` and the `networkproto1.c` DDC configuration were validated against this hardware.
>
> Other HPSDR-compatible radios that support 7 or more DDCs (e.g., ANAN-7000DLE, ANAN-8000DLE, Orion MkII) use the same HPSDR Protocol 2 router tables and may work, but have **not been tested**. Radios with fewer DDCs (ANAN-10, ANAN-100, Hermes, ANAN-G2E) do not expose receivers beyond RX2 and are not compatible with the headless multi-port feature.
>
> The headless TCI server requires a hardware receiver capable of providing at least 3 DDC streams. On 2-DDC hardware, ports 50003–50008 will accept connections and negotiate, but no audio will flow.

---

## 1. Overview & Operational Architecture

Branch F adds a parallel TCI server infrastructure alongside the existing TCI server on port 50001. Each headless port presents itself as an independent single-TRX radio, mapped one-to-one to a hardware DDC:

| Port | TCI RX | DDC | Source | Description |
| :--- | :--- | :--- | :--- | :--- |
| 50001 | RX1 (TRX 0), RX2 (TRX 1) | DDC0, DDC1 | `TCIServer.cs` (original) | Full-capability main operator position. WSJT-X can connect here as RX2 using TRX 1. |
| 50002 | RX2 (TRX 0) | DDC1 | `HeadlessTciServer.cs` | Headless — dedicated single-TRX entry point for RX2 (same receiver as port 50001 TRX 1, simplified interface) |
| 50003 | RX3 (TRX 0) | DDC2 | `HeadlessTciServer.cs` | Headless — digital slice |
| 50004 | RX4 (TRX 0) | DDC3 | `HeadlessTciServer.cs` | Headless — digital slice |
| 50005 | RX5 (TRX 0) | DDC4 | `HeadlessTciServer.cs` | Headless — digital slice |
| 50006 | RX6 (TRX 0) | DDC5 | `HeadlessTciServer.cs` | Headless — digital slice |
| 50007 | RX7 (TRX 0) | DDC6 | `HeadlessTciServer.cs` | Headless — digital slice |
| 50008 | RX8 (TRX 0) | DDC7 | `HeadlessTciServer.cs` | Headless — digital slice |

Each headless port advertises `trx_count:1` and `channels_count:1`, presenting a simplified single-receiver interface to connecting clients. Port 50002 exposes the same RX2 receiver that is also available as TRX 1 on port 50001 — but through the headless server with its simplified capability set.

---

## 2. Key New Components

### 2.1 HeadlessTciServer.cs (1,722 lines)

A complete from-scratch WebSocket TCI server implementation, independent from the original `TCIServer.cs`. Key features:

- **WebSocket handshake**: Full RFC 6455 upgrade with `Sec-WebSocket-Key` / `SHA-1` / `Sec-WebSocket-Accept` flow.
- **Initial banner**: Sends `protocol`, `device`, `trx_count`, `channels_count`, `vfo_limits`, `if_limits`, `modulations_list`, `iq_samplerate`, `audio_samplerate`, `audio_stream_sample_type`, `audio_stream_channels`, `audio_stream_samples`, `tx_stream_audio_buffering`, VFO frequency, modulation, filter band, `rx_enable`, `tx_enable`, `split_enable`, `rit_enable`, `xit_enable`, `lock`, `sql_enable`, `trx`, `drive`, `tune_drive`, `mute`, `start`, `ready`.
- **RX audio streaming**: Binary `RX_AUDIO_STREAM` frames with ring-buffered accumulation (32,768-sample zero-alloc circular buffer), configurable sample format (float32/int16/int24/int32), 1 or 2 channels, configurable packet size (100–2048 samples), 48 kHz target rate.
- **TX audio ingestion**: Decodes inbound `TX_AUDIO_STREAM` binary frames from clients (WSJT-X etc.), supports modern channel-count header semantics and legacy length-only mode, clamps/sanitizes samples, queues with overflow protection (32 blocks / 65,536 complex samples max).
- **TX_CHRONO flow control**: Sends `TX_CHRONO` binary frames to pace client audio transmission per TCI 2.0 protocol requirements.
- **Dedicated sender thread**: Background `AboveNormal` priority thread with a queued outbound frame buffer (capped at 32 frames ≈1.3s of audio) ensures network I/O never blocks the real-time DSP audio callback thread.

### 2.2 HeadlessSliceManager.cs (321 lines)

Manages on-demand activation/deactivation of headless receiver slices (RX2–RX8 / DDC 1–7):

- **Dormant by default**: Slices consume 0% DSP CPU when no client is streaming audio.
- **Activation**: On `audio_start`, loads the ChannelMaster router, sets DDC rate, configures WDSP DSP parameters (mode, bandpass, NBP, SNBA, AGC, panel gain), and turns on the WDSP channel.
- **Deactivation**: On `audio_stop` (when no other client is streaming), turns off the WDSP channel to release DSP resources.
- **State management**: Tracks frequency, mode, filter bandwidth, audio gain, and active/streaming state per slice.
- **Hardware integration**: Sets VFO frequency via `NetworkIO.VFOfreq()`, mode/filter via direct WDSP API calls.

### 2.3 TxArbiter.cs (304 lines)

Central TX arbitration and interlock guaranteeing safe coexistence of voice and digital transmissions:

- **Voice priority**: Mic VOX, Mic PTT, or GUI MOX immediately preempts any active digital TX. The preempted client receives `trx:0,false`.
- **Digital LIFO**: Last digital transmitter wins; previous digital TX is preempted.
- **Single digital TX**: Only one digital slice may transmit at a time across all 7 headless ports.
- **Full-duplex preservation**: `Audio.MOX` remains `false` during digital TX, so the Red Pitaya receiver pipeline stays active and other slices continue receiving.
- **DSP TX engagement**: On `RequestDigitalTx`, saves voice DSP mode, switches to slice mode (DIGU), enables WDSP transmitter channel (`SetChannelState`), engages ChannelMaster TCI TX audio runner, and wakes the pacing thread.
- **State restoration**: On release/preemption, restores voice mode, disables TX DSP channel, flushes queues, releases CI-V digital steering.
- **CI-V coordination**: Coordinates with `CIVController` for radio frequency steering, PTT keying, and full state snapshot/restore around digital TX sessions.

### 2.4 ITciTxAudioSource Interface (TCIServer.cs)

A unified interface abstraction allowing `cmaster.cs` to source TX audio from either the original TCI server (port 50001) or the headless TCI manager (ports 50002–50008):

- `UsesActiveTCITxAudio()` — Is TX audio currently being sourced?
- `TryGetTxAudioRequestSettings()` — Get sample rate, samples, buffering
- `SendTxChrono()` — Send flow-control pacing frame
- `TryDequeueTxAudio()` — Dequeue next TX audio block

Implemented by both `TCPIPtciServer` (original) and `HeadlessTciManager` (headless).

### 2.5 cmaster.cs — Multi-Source TX Pacing

Generalized `serviceTCITxProtocol()` to dynamically select the TX audio source:
- If `TxArbiter.Instance.ActiveDigitalRx != -1`: routes from `HeadlessTciManager.Instance`
- Else if `Audio.MOX == true`: routes from `TCIServer` (original)

Dequeues audio, resamples to native transmitter input rate, and feeds into the WDSP TX pipeline via `InboundTCITxAudio` delegate → `fexchange0` → `xsidetone` → `xvacOUT` to Processed TX Output / IC-7100 USB Audio.

---

## 3. Audio Level Management

### Full TCI Server (port 50001) — Fully Client-Controllable

The original server exposes three independent gain stages, all controllable by the TCI client:

| Control | TCI Command | What It Does | Range |
| :--- | :--- | :--- | :--- |
| AGC mode | `agc_mode:0,fast` | Sets AGC timing: OFF/LONG/SLOW/MED/FAST/CUSTOM | 6 modes |
| AGC on/off | `agc_auto_ex:0,false` | Enables/disables AGC | true/false |
| AGC manual gain | `agc_gain:0,80` | Manual gain when AGC is off/auto | -20 to +120 dB |
| RX volume | `rx_volume:0,0,-10.0` | Per-receiver output gain (PanelGain1) | 0 dB = unity (1.0), -60 dB = muted |
| Master AF volume | `volume:-8` | Global AF volume applied after per-receiver gain | 0 dB = unity, -60 dB = muted |

The AGC top (maximum gain ceiling) and default mode are set by the operator in the Thetis UI and can be overridden by the TCI client. At `rx_volume:0,0,0` (0 dB), the output gain is unity (1.0) — full volume with no attenuation.

### Headless TCI Server (ports 50002–50008) — Hardcoded AGC + Calibrated Output

| Control | Status | Details |
| :--- | :--- | :--- |
| AGC mode | **Hardcoded MED** | Set once on slice activation, not changeable by client |
| AGC on/off | **Always on** | No `agc_auto_ex` handler |
| AGC manual gain | **Not available** | No `agc_gain` handler |
| AGC top | **Hardcoded 90 dB** | Maximum gain ceiling the AGC can apply for weak signals |
| RX volume | **Partially controllable** | `rx_volume` adjusts output, but with a fixed -26 dB calibration offset |
| Master AF volume | **Ignored** | Always echoes `volume:0`, no effect |

The headless audio chain is:

```
Raw IQ → [DSP: shift, resample, bandpass, NR, AGC(MED, top=90dB), filter, squelch] → Panel Gain (×0.05) → Output
```

**AGC** is active and provides automatic leveling — it continuously adapts gain based on signal strength (up to 90 dB of gain for very weak signals). This is not a fixed 90 dB gain; 90 dB is the ceiling on how much gain the AGC may apply.

**Panel Gain** applies a fixed 0.05× attenuation (-26 dB) after AGC. This calibration prevents clipping when streaming to digital mode software. The `rx_volume` command scales this:

$$\text{gainFactor} = 10^{\text{volDb}/20} \times 0.05$$

| `rx_volume` value | Full server gain | Headless gain | Headless level |
| :--- | :--- | :--- | :--- |
| 0 dB (default) | 1.0 (unity) | 0.05 | -26 dB |
| +6 dB | 2.0 | 0.10 | -20 dB |
| -14 dB | 0.2 | 0.01 | -40 dB |
| -26 dB | 0.05 | 0.0025 | -52 dB |

At `rx_volume:0,0,0` (the value WSJT-X sends by default), the full server outputs unity gain (1.0, full volume). The headless server outputs 0.05 (-26 dB) — already attenuated to a safe level for digital mode software.

### Rationale for Hardcoded AGC

The headless server is designed for "set and forget" digital mode operation. FT8/JS8/RTTY work well with MED AGC and -26 dB attenuation. Allowing per-client AGC control would add complexity for limited benefit in the target use case. For operators who need full AGC control (e.g., CW skimming with AGC off), the original server on port 50001 remains available.

---

## 4. Capability Comparison: Port 50001 vs. Ports 50003–50008

### Fully supported on headless ports

| Capability | Details |
| :--- | :--- |
| RX audio streaming | Binary frames, float32/int16/int24/int32, 1–2 channels, 48 kHz, configurable packet size |
| TX audio (inbound) | Binary `TX_AUDIO_STREAM` frames decoded, queued, fed to DSP TX pipeline |
| TX_CHRONO flow control | Server-paced chrono frames for client audio timing |
| VFO frequency | Set/query via `vfo:` command → `NetworkIO.VFOfreq()` |
| Modulation | DIGU, DIGL, USB, LSB, CWU, CWL, AM, SAM, FM/NFM |
| RX filter band | `rx_filter_band` → WDSP bandpass/NBP/SNBA |
| PTT (trx) | `trx:0,bool` — arbitrated by TxArbiter |
| Tune | `tune:0,bool` — same arbitration path |
| RX volume / gain | `rx_volume` → `WDSP.SetRXAPanelGain1()` with -26 dB calibration (see §3) |
| Stream format negotiation | `audio_stream_sample_type`, `audio_stream_channels`, `audio_stream_samples`, `tx_stream_audio_buffering` |
| Audio sample rate | `audio_samplerate` negotiation |
| Start/Stop (power) | `start`/`stop` toggles radio power |
| Full handshake | Init banner + `ready` |

### Limited / echo-only on headless ports

| Capability | Status | Notes |
| :--- | :--- | :--- |
| Split | Echo only | Always echoes `false` |
| RIT/XIT enable | Echo only | Accepted, not applied |
| RIT/XIT offset | Echo only | Accepted, not applied |
| Squelch enable/level | Echo only | Accepted, not applied |
| Lock | Echo only | Accepted, not applied |
| Drive / tune_drive | Echo only | Accepted, not applied to hardware |
| rx_enable / tx_enable | Echo only | Acknowledged |
| rx_channel_enable | Echo only | Acknowledged, no sub-RX support |
| Mute / volume | Stub | `mute` → `false`, `volume` → `0` |
| CW macros / keyer | Stub | Echoes fixed speed (30), no keyer control |
| IQ sample rate | Echo only | Echoed, but no IQ stream sent |

### Not supported on headless ports (available on port 50001 only)

| Capability | Notes |
| :--- | :--- |
| IQ streaming | No `iq_start`/`iq_stop` handler; no `PublishIQSamples`. CW Skimmers and panadapter clients requiring raw IQ will not work on headless ports. |
| Sub-RX / channels | Single channel only (`channels_count:1`) |
| Second TRX | Single TRX per port (`trx_count:1`) |
| AGC control | Hardcoded to MED mode, top=90 dB. No `agc_mode`/`agc_gain`/`agc_auto_ex` handling. (See §3 for details.) |
| Noise blanker | No `rx_nb_enable`/`rx_nb2_enable` handling |
| Noise reduction | No `rx_nr_enable`/`rx_nr_enable_ex` handling |
| Binaural (BIN) | No `rx_bin_enable` handling |
| Auto notch (ANF) | No `rx_anf_enable` handling |
| APF / NF / DSE | Not handled |
| DDS (panadapter center) | Not handled |
| VFO lock (per-VFO) | Not handled |
| VFO swap | Not handled |
| Line out (VAC control) | Not handled — VAC1/VAC2 are global shared resources already dedicated to Processed TX Output and RX2 audio. Headless ports have their own TX audio path that bypasses VAC entirely. |
| Spots | Not handled |
| TX profiles | Not handled |
| Sensors / S-meter | No periodic sensor reporting |
| Thetis extensions | `rx_ctun_ex`, `calibration_ex`, `run_cat_ex`, `shutdown_ex`, `fm_deviation_ex`, `rx_step_att_ex`, `rx_preamp_att_ex`, `vfo_sync_ex` — not handled |

---

## 5. TX Arbitration Model

The `TxArbiter` implements a priority-based TX interlock unique to the headless ports:

1. **Voice has absolute priority** — Mic VOX, Mic PTT, or GUI MOX immediately preempts any active digital TX on any headless port. The preempted client receives `trx:0,false`.
2. **Digital transmissions are LIFO** — the last digital client to request TX wins; the previous one is preempted.
3. **Only one digital TX at a time** across all 7 headless ports.
4. **Full-duplex preserved** — `Audio.MOX` stays `false` during digital TX so RX continues on all ports.
5. **CI-V state snapshot/restore** — Before digital TX, the `CIVController` snapshots VFO A/B frequencies, modes, filters, split state, and selected VFO. After TX, all state is restored and the IC-7100 is returned to its pre-TX configuration.

---

## 6. Verification Results

> All tests were performed against **live Red Pitaya hardware** (192.168.129.52) running OpenHPSDR-compatible firmware with 8 DDC support. No other radio hardware was used.

### Multi-Channel Audio Streaming (all 8 ports)

Tested with `test_all_channels_audio.py` — 8-second streaming per port:

| Port | RX | Packets | Bandwidth | Interval (avg) | Dropouts | Result |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 50001 | RX1 | 188 | 376.5 KB/s | 42.7 ms | 0 | PASSED |
| 50002 | RX2 | 188 | 375.7 KB/s | 42.7 ms | 0 | PASSED |
| 50003 | RX3 | 188 | 376.4 KB/s | 42.7 ms | 0 | PASSED |
| 50004 | RX4 | 188 | 376.5 KB/s | 42.7 ms | 0 | PASSED |
| 50005 | RX5 | 188 | 376.4 KB/s | 42.7 ms | 0 | PASSED |
| 50006 | RX6 | 188 | 376.5 KB/s | 42.7 ms | 0 | PASSED |
| 50007 | RX7 | 188 | 376.4 KB/s | 42.7 ms | 0 | PASSED |
| 50008 | RX8 | 188 | 376.5 KB/s | 42.7 ms | 0 | PASSED |

### Concurrent Streaming (ports 50001 + 50008 simultaneously)

10-second simultaneous streaming, independent WebSocket connections:

| Port | Packets | Throughput | Dropouts | Status |
| :--- | :--- | :--- | :--- | :--- |
| 50001 | 235 | 376.4 KB/s | 0 | PASSED |
| 50008 | 235 | 376.0 KB/s | 0 | PASSED |

### WSJT-X TX Test (port 50008)

Tested with `test_tune_port_50008.py` — simulated WSJT-X Tune sequence:

- Handshake: OK
- RX audio streaming: 24 packets/sec
- TX keying (`trx:0,true` + `tune:0,true`): Server confirmed
- TX_CHRONO packets received: 11
- Audio packets sent to Thetis: 11 (100% ingested)
- RX audio during TX (full-duplex): 71 packets, uninterrupted
- Unkey: OK

---

## 7. Source Tree & Commits

- **Repository**: [satfan52/Thetis](https://github.com/satfan52/Thetis)
- **Branch**: `F`
- **Base**: Branched from Branch E (`e502c1b`)
- **Key Source Files**:
  - `Project Files/Source/Console/HeadlessTciServer.cs`: Multi-port WebSocket TCI server, client handler, binary frame encoding/decoding, TX_CHRONO flow control.
  - `Project Files/Source/Console/HeadlessSliceManager.cs`: On-demand DDC slice activation/deactivation, WDSP DSP configuration.
  - `Project Files/Source/Console/TxArbiter.cs`: Voice/digital TX arbitration, DSP channel engagement, CI-V steering coordination.
  - `Project Files/Source/Console/TCIServer.cs`: `ITciTxAudioSource` interface, promoted enum/class visibility.
  - `Project Files/Source/Console/cmaster.cs`: Multi-source TX pacing, `HeadlessAudioPublisher` callback, resampled audio queueing.
  - `Project Files/Source/Console/console.cs`: Digital TX DDS frequency steering, MOX change handling, panadapter display protection.
  - `Project Files/Source/Console/CAT/CIVController.cs`: Digital TX state snapshot/restore, status report guarding, dynamic frequency updates.
  - `Project Files/Source/ChannelMaster/cmaster.c`: Native TCI audio export callback.
  - `Project Files/Source/ChannelMaster/networkproto1.c`: 8-receiver router pipeline.
  - `Project Files/Source/ChannelMaster/netInterface.c`: DDC rate configuration for headless slices.

---

## 8. Branch Lineage

```
master
  └── feature/dedicated-processed-tx-output (Branch D)
        └── E (CI-V + Processed TX Output)
              └── F (Multi-Port Headless TCI Server)
```

See also:
- [Branch E documentation](HYBRID-SDR-BRANCH-E.md)
- [Branch D documentation](HYBRID-SDR-BRANCH-D.md)
