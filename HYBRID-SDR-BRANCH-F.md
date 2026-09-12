# Hybrid SDR Branch F — Multi-Port Headless TCI Server (RX3–RX8) & Digital TX Flow Control

Branch F extends Branch E with a **multi-port headless TCI server** that exposes RX3 through RX8 on dedicated WebSocket ports (50003–50008), enabling independent digital mode applications (WSJT-X, JTDX, JS8Call, etc.) to connect directly to individual receivers without conflicting with the main operator position on port 50001.

---

## 1. Overview & Operational Architecture

Branch F adds a parallel TCI server infrastructure alongside the existing TCI server on port 50001. Each headless port presents itself as an independent single-TRX radio, mapped one-to-one to a hardware DDC:

| Port | TCI RX | DDC | Source | Description |
| :--- | :--- | :--- | :--- | :--- |
| 50001 | RX1 | DDC0 | `TCIServer.cs` (original) | Full-capability main operator position |
| 50002 | RX2 | DDC1 | `HeadlessTciServer.cs` | Headless — second receiver |
| 50003 | RX3 | DDC2 | `HeadlessTciServer.cs` | Headless — digital slice |
| 50004 | RX4 | DDC3 | `HeadlessTciServer.cs` | Headless — digital slice |
| 50005 | RX5 | DDC4 | `HeadlessTciServer.cs` | Headless — digital slice |
| 50006 | RX6 | DDC5 | `HeadlessTciServer.cs` | Headless — digital slice |
| 50007 | RX7 | DDC6 | `HeadlessTciServer.cs` | Headless — digital slice |
| 50008 | RX8 | DDC7 | `HeadlessTciServer.cs` | Headless — digital slice |

Each headless port advertises `trx_count:1` and `channels_count:1`, presenting a simplified single-receiver interface to connecting clients.

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

## 3. Capability Comparison: Port 50001 vs. Ports 50003–50008

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
| RX volume / gain | `rx_volume` → `WDSP.SetRXAPanelGain1()` with -26 dB calibration |
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
| AGC control | Hardcoded to MED mode, top=90 dB. No `agc_mode`/`agc_gain`/`agc_auto_ex` handling. |
| Noise blanker | No `rx_nb_enable`/`rx_nb2_enable` handling |
| Noise reduction | No `rx_nr_enable`/`rx_nr_enable_ex` handling |
| Binaural (BIN) | No `rx_bin_enable` handling |
| Auto notch (ANF) | No `rx_anf_enable` handling |
| APF / NF / DSE | Not handled |
| DDS (panadapter center) | Not handled |
| VFO lock (per-VFO) | Not handled |
| VFO swap | Not handled |
| Line out (VAC control) | Not handled |
| Spots | Not handled |
| TX profiles | Not handled |
| Sensors / S-meter | No periodic sensor reporting |
| Thetis extensions | `rx_ctun_ex`, `calibration_ex`, `run_cat_ex`, `shutdown_ex`, `fm_deviation_ex`, `rx_step_att_ex`, `rx_preamp_att_ex`, `vfo_sync_ex` — not handled |

---

## 4. TX Arbitration Model

The `TxArbiter` implements a priority-based TX interlock unique to the headless ports:

1. **Voice has absolute priority** — Mic VOX, Mic PTT, or GUI MOX immediately preempts any active digital TX on any headless port. The preempted client receives `trx:0,false`.
2. **Digital transmissions are LIFO** — the last digital client to request TX wins; the previous one is preempted.
3. **Only one digital TX at a time** across all 7 headless ports.
4. **Full-duplex preserved** — `Audio.MOX` stays `false` during digital TX so RX continues on all ports.
5. **CI-V state snapshot/restore** — Before digital TX, the `CIVController` snapshots VFO A/B frequencies, modes, filters, split state, and selected VFO. After TX, all state is restored and the IC-7100 is returned to its pre-TX configuration.

---

## 5. Verification Results

### Multi-Channel Audio Streaming (all 8 ports)

Tested with `test_all_channels_audio.py` — 8-second streaming per port against live Red Pitaya hardware:

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

## 6. Source Tree & Commits

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

## 7. Branch Lineage

```
master
  └── feature/dedicated-processed-tx-output (Branch D)
        └── E (CI-V + Processed TX Output)
              └── F (Multi-Port Headless TCI Server)
```

See also:
- [Branch E documentation](HYBRID-SDR-BRANCH-E.md)
- [Branch D documentation](HYBRID-SDR-BRANCH-D.md)
