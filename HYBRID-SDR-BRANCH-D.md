# Hybrid SDR Branch D — dedicated processed-TX output

Branch D implements an independent post-TX-DSP audio output for the Red Pitaya RX + Thetis DSP + IC-7100 RF TX configuration.

## Status

Hardware validation completed successfully on 4 September 2026 using the Red Pitaya / IC-7100 setup.

Validated result:

- VAC1 remains available for the microphone input.
- The normal Thetis microphone, compressor, and TX DSP processing remain in the transmit path.
- **Setup → Audio → TX Output** sends the processed transmit audio to the IC-7100 audio device.
- VAC2 remains unchanged and available for its normal RX2/second-receiver role.
- The dedicated TX Output enable, device, buffer, sample-rate, gain, and WASAPI Exclusive Out controls operate independently of VAC2.

The Setup page is declared in `setup.designer.cs` and added during `InitializeComponent()`. This corrects the earlier D package in which the code was compiled but the dynamically created tab was not exposed by the built Setup interface.

## Install the compiled version

1. Open the [Hybrid SDR A/B/C/D release page](https://github.com/satfan52/Thetis/releases/tag/hybrid-sdr-testset-2026-09-03).
2. Download **Thetis-HybridSDR-D-Dedicated-TX-Output-Designer-Fix-x64.zip**.
3. Extract it into a new, separate folder.
4. Run `Thetis.exe` from that folder.
5. Confirm that **TX Output** appears under **Setup → Audio**, between **VAC 2** and **Options**.

Do not merge A, B, C, and D into the same extracted directory.

## Configure Branch D

1. Configure the microphone normally through VAC1.
2. Open **Setup → Audio → TX Output**.
3. Select the required audio driver.
4. Select the IC-7100 output device, normally identified as **USB Audio CODEC**.
5. Select the buffer size and sample rate required by the device; 512 samples and 48000 Hz are appropriate starting values.
6. Set the dedicated TX output gain; 0 dB is the recommended starting point.
7. Enable **WASAPI Exclusive Out** only if exclusive access is desired and the device is not needed by another application.
8. Select **Enable Processed TX Output**.

VAC2 does not need to be enabled or repurposed for this route.

## Source and scope

- Branch: [feature/dedicated-processed-tx-output](https://github.com/satfan52/Thetis/tree/feature/dedicated-processed-tx-output)
- Designer-fix implementation commit: [79822dad](https://github.com/satfan52/Thetis/commit/79822dad89b9cd94b0a93934c247ffbcbcf7f514)
- Compiled packages: [Hybrid SDR A/B/C/D test-set release](https://github.com/satfan52/Thetis/releases/tag/hybrid-sdr-testset-2026-09-03)

This is an experimental hardware-tested fork and not an official Thetis release.
