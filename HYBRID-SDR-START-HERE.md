# Thetis Hybrid SDR A/B/C frozen test set

This project is an experimental Thetis fork for a hybrid SDR architecture in which a Red Pitaya is used as the HF receiver, Thetis performs RX and TX DSP, and an Icom IC-7100 is used as the physical SSB transmitter through its USB Audio CODEC.

## Frozen product states

### A — Untouched control
- Branch: `master`
- Commit: `852bf0ef0b4f3886a13fc2846489aee16f361872`
- Purpose: upstream-equivalent Thetis control/reference. No hybrid-SDR modifications.

### B — Phase 1 diagnostic
- Branch: `feature/independent-tx-audio-routing-phase1`
- Commit: `591ee826db17acef1df6caed40d3bbe68251ad55`
- Parent: A
- Purpose: hardware-test the existing post-TX-DSP audio path through VAC2 using the earlier duplex diagnostic implementation. This is intentionally not the final UI.

### C — Phase 2 complete
- Branch: `feature/independent-tx-audio-routing`
- Commit: `8f7c6058477899b0e513c28ab9a87f14d46192e7`
- Parent: B
- Purpose: complete reversible implementation. VAC2 can operate either as ordinary **Normal VAC** or as **Processed TX Output** using a true PortAudio output-only stream with its own output settings and gain.

## Intended architecture

RX:
`Antenna -> Red Pitaya ADC -> HPSDR/Hermes Ethernet IQ -> Thetis RX DSP -> PC speakers`

TX:
`PC microphone -> Thetis TX DSP -> post-DSP TX speech PCM -> VAC2 -> IC-7100 USB Audio CODEC -> Icom SSB modulator/PA -> RF`

The Red Pitaya is not intended to generate RF in this configuration.

## Validation status at freeze

- A: Release x64 compiled successfully and was launched successfully on Windows.
- B: Release x64 compiled successfully. Hardware signal-path testing pending.
- C: Release x64 compiled successfully; GUI was launched and visually checked successfully on Windows. Hardware signal-path testing pending.

These are experimental builds, not official Thetis releases. Full Red Pitaya + IC-7100 hardware validation is deliberately the next stage.

## Test order

On a new test PC, use **A -> B -> C**. A establishes the Red Pitaya/Thetis control environment; B isolates the diagnostic TX-audio path; C tests the intended complete implementation.

For frozen ready-to-run binaries, use the project test release rather than recompiling. Extract A, B and C to separate folders and run `Thetis.exe` directly. Do not overwrite one build with another.

See `HARDWARE-TEST-CHECKLIST.md` on this documentation branch for the controlled hardware procedure.

## Safety

The Red Pitaya TX/RF path should be physically disconnected or otherwise protected during this hybrid test. If the receiver and IC-7100 share an antenna system, suitable hardwired T/R sequencing and receiver-front-end protection are mandatory. Initial Icom transmission tests should use low RF power and preferably a dummy load. PureSignal should remain OFF for this architecture.
