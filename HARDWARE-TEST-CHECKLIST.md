# Thetis Hybrid SDR — A/B/C hardware test checklist

## Purpose
Validate the frozen A/B/C binaries without recompiling them. Test in order **A -> B -> C** so a new Windows PC, Red Pitaya configuration, USB audio, and the code changes are not all treated as one variable.

## Before testing
- Download all three frozen ZIPs and the SHA-256 checksum file. Verify the hashes.
- Extract A, B and C into **three separate folders**. Do not copy one over another.
- Be aware that Thetis builds may use the same user settings/database. Back up existing Thetis settings before changing VAC configuration.
- Confirm the Red Pitaya transmit/RF output is physically disconnected or otherwise made safe. Do not rely only on a software DRIVE setting.
- If Red Pitaya and IC-7100 share an antenna system, use suitable hardwired T/R sequencing and receiver-front-end protection.
- Initial IC-7100 RF tests: low power and preferably a dummy load. PureSignal OFF.
- IC-7100: ordinary USB/LSB voice mode, DATA mode OFF; `DATA OFF MOD = USB`; Icom compressor OFF and TX bass/treble neutral initially; start `USB MOD Level` low.
- Windows should show the IC-7100 USB audio endpoints, normally `Speakers (USB Audio CODEC)` and `Microphone (USB Audio CODEC)`.
- Thetis TX microphone for these tests is the **PC/local microphone**, not VAC2.

## A — Untouched control
Branch `master`, commit `852bf0ef0b4f3886a13fc2846489aee16f361872`.

Record:
- Thetis launches normally.
- Red Pitaya is discovered/connects.
- Panadapter/waterfall and tuning operate normally.
- RX DSP/audio works through the intended PC speakers.
- No unusual freezes, audio errors, or network instability.

Do not proceed to interpreting B/C failures until A is a stable control on this PC.

## B — Phase 1 diagnostic
Branch `feature/independent-tx-audio-routing-phase1`, commit `591ee826db17acef1df6caed40d3bbe68251ad55`.

This build intentionally uses VAC2 as a duplex diagnostic processed-TX path; it is not the final UI.

Suggested setup:
- Keep the same Red Pitaya/RX configuration established with A.
- Enable VAC2.
- VAC2 output: IC-7100 `Speakers (USB Audio CODEC)`.
- Because B is still duplex, choose a valid VAC2 input device; the Icom `Microphone (USB Audio CODEC)` is suitable for the test even though it is not used as the TX microphone source.
- Direct I/Q OFF; RX2/split complications OFF for the first test.
- Main Thetis MON button can remain OFF. Set the Thetis monitor-volume value to a moderate non-zero level because B uses that value as the diagnostic VAC2 TX level.

Tests:
- In receive, VAC2 must not feed ordinary Red Pitaya RX audio into the Icom modulation path.
- Put Thetis into MOX so its TX DSP runs. Key the IC-7100 separately for the initial test and speak into the PC microphone.
- Confirm the Icom receives/modulates the speech audio.
- Change a clearly audible Thetis TX processing control (TX EQ, COMP/CFC, etc.) and confirm the Icom-transmitted audio changes. This is the key proof that the audio is post-Thetis-TX-DSP.
- Repeat MOX on/off several times. Watch specifically for VAC2 stalls, clicks, underruns, freezes, or failure to return cleanly to RX.

## C — Phase 2 complete
Branch `feature/independent-tx-audio-routing`, commit `8f7c6058477899b0e513c28ab9a87f14d46192e7`.

First verify **Normal VAC**:
- Start C and select VAC2 `Normal VAC`.
- Confirm ordinary VAC2 controls/behavior are present and that normal settings can be used.

Then verify **Processed TX Output**:
- Select VAC2 `Processed TX Output`.
- Confirm the GUI indicates there is no input device / output-only operation.
- Output: IC-7100 `Speakers (USB Audio CODEC)`.
- Set a conservative sample rate/buffer initially (48 kHz, 512 is a stability-first choice).
- Start TX output gain conservatively.
- Confirm Red Pitaya RX audio does not leak to the Icom output in receive.
- Run Thetis TX DSP with MOX, key the Icom, and confirm processed speech reaches the transmitter.
- Again change TX EQ/COMP/CFC and confirm the transmitted audio follows the Thetis processing.
- Repeat TX/RX transitions and look for underruns/stalls.
- Switch back to `Normal VAC`; confirm the ordinary VAC2 configuration/controls return and the normal configuration has not been destroyed.

## Evidence to record for each build
Record PASS/FAIL plus a short note for: launch; Red Pitaya connection; RX stability; RX audio; VAC2 behavior in receive; MOX start; Icom modulation; audible response to Thetis TX DSP changes; MOX release/return to RX; clicks/underruns/stalls; and any error message. Screenshots of VAC2 settings and any error dialog are useful.

If C fails, compare with B before changing code. If B also fails but A is stable, diagnose/fix the Phase-1 signal path first, then rebase/reapply Phase 2 on the corrected Phase 1.
