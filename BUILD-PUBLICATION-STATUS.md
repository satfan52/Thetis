# Hybrid SDR A/B/C/D publication status

Publication is complete for four reproducible hardware-test states:

- A — untouched upstream-equivalent control (`852bf0ef0b4f3886a13fc2846489aee16f361872`)
- B — Phase 1 processed-TX diagnostic (`591ee826db17acef1df6caed40d3bbe68251ad55`)
- C — corrected reversible VAC2 implementation (`5153394ce829f5979c44cae626789ebdb3455d9e`)
- D — dedicated processed-TX output with VAC2 preserved (`27d6b0ad64a737ac0cf385a6cb03e23da0612a35`)

Branch D was successfully hardware-tested on 4 September 2026 with the Red Pitaya and IC-7100. VAC1 microphone audio passes through normal Thetis TX processing, the independent TX Output feeds the IC-7100, its gain and the Thetis compressor are effective, and VAC2 remains available for RX2.

Release and compiled downloads: https://github.com/satfan52/Thetis/releases/tag/hybrid-sdr-testset-2026-09-03

Branch D instructions: https://github.com/satfan52/Thetis/blob/feature/dedicated-processed-tx-output/HYBRID-SDR-BRANCH-D.md

This branch contains publication automation only. Do not use it as a radio-testing source branch.
