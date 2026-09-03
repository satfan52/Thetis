# Thetis Hybrid SDR experiment — START HERE

This documentation branch describes an **experimental hybrid-SDR project** built on Thetis. The objective is to use a **Red Pitaya as the HF receiver**, keep **Thetis for RX DSP and the complete TX speech-processing chain**, and use an **Icom IC-7100 as the physical RF transmitter** through its USB Audio CODEC.

This is **not an official Thetis release**. The complete Red Pitaya + IC-7100 signal path is still awaiting hardware validation.

## Intended architecture

**RX**  
`Antenna -> Red Pitaya ADC -> HPSDR/Hermes Ethernet IQ -> Thetis RX DSP -> PC speakers`

**TX**  
`PC microphone -> Thetis TX DSP -> post-DSP TX speech PCM -> VAC2 -> IC-7100 USB Audio CODEC -> Icom SSB modulator/PA -> RF`

The Red Pitaya is intended to remain RX-only in this configuration.

## Frozen A/B/C source states

| State | Branch | Frozen commit | Meaning |
|---|---|---|---|
| **A** | `master` | `852bf0ef0b4f3886a13fc2846489aee16f361872` | Untouched upstream-equivalent Thetis control |
| **B** | `feature/independent-tx-audio-routing-phase1` | `591ee826db17acef1df6caed40d3bbe68251ad55` | Phase-1 duplex VAC2 processed-TX diagnostic |
| **C** | `feature/independent-tx-audio-routing` | `8f7c6058477899b0e513c28ab9a87f14d46192e7` | Complete reversible Phase-2 implementation |

### B — Phase 1 diagnostic
B is intentionally a diagnostic intermediate build. It exposes the existing post-TX-DSP TX-monitor audio through VAC2 using the existing duplex IVAC/PortAudio mechanism. Its purpose is fault isolation, not the final user interface.

### C — Phase 2 complete
C is the intended implementation. VAC2 has two operating modes:

- **Normal VAC** — ordinary legacy duplex VAC2 behavior and settings.
- **Processed TX Output** — a true PortAudio output-only stream carrying post-Thetis-TX-DSP speech, with no VAC2 input device and separate output device/sample-rate/buffer/exclusive-output/gain settings.

Switching back to Normal VAC restores ordinary VAC2 operation.

## Download ready-to-run binaries

The frozen GitHub prerelease is here:

**https://github.com/satfan52/Thetis/releases/tag/hybrid-sdr-testset-2026-09-03**

It contains ready-to-run x64 ZIPs for A, B and C plus SHA-256 checksums. **No compilation is required for hardware testing.** Extract each ZIP into a separate folder and run `Thetis.exe` directly.

The publication workflow independently checked out the exact frozen A/B/C commits, compiled all three successfully in Release x64, verified the executable was packaged at ZIP root, and only then published the prerelease.

## Recommended test sequence

Use **A -> B -> C** on the Red-Pitaya test PC.

1. **A** proves the PC, Red Pitaya connection, Thetis receiver, network path and PC audio with untouched Thetis.
2. **B** tests the basic post-TX-DSP VAC2 diagnostic route.
3. **C** tests the complete reversible Processed TX Output implementation.

See **[HARDWARE-TEST-CHECKLIST.md](HARDWARE-TEST-CHECKLIST.md)** for the controlled test procedure.

## Validation status at freeze

- **A:** compiled successfully and launched successfully on Windows.
- **B:** compiled successfully; hardware signal-path test pending.
- **C:** compiled successfully; the Phase-2 GUI was launched and visually inspected successfully on Windows; hardware signal-path test pending.

## Why the documentation is on a separate branch

`master` deliberately remains at the exact untouched upstream reference commit so it can serve as State A. The documentation is therefore kept on `docs/hybrid-sdr-project` rather than modifying the control branch.

## RF safety

Physically disconnect or otherwise protect the Red Pitaya TX/RF path for the initial hybrid tests; do not rely only on a software DRIVE control. If the Red Pitaya and IC-7100 share an antenna system, suitable hardwired T/R sequencing and receiver-front-end protection are required. Start IC-7100 RF testing at low power and preferably into a dummy load. PureSignal should remain OFF for this hybrid architecture.

Related upstream discussion: `ramdor/Thetis#544`, “Limit TX audio monitor on VAC1 and/or 2”.
