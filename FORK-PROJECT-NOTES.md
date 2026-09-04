# Hybrid SDR fork project notes

The published test set contains four frozen states:

- A — untouched control
- B — Phase 1 diagnostic VAC2 processed-TX route
- C — corrected reversible VAC2 processed-TX implementation
- D — dedicated processed-TX output; VAC2 remains standard

D is the recommended final design and was successfully validated with the Red Pitaya and IC-7100 on 4 September 2026. Its independent output is configured under Setup -> Audio -> TX Output. VAC1 remains the microphone input, normal Thetis TX DSP remains active, and VAC2 is free for RX2.

Compiled A/B/C/D downloads: https://github.com/satfan52/Thetis/releases/tag/hybrid-sdr-testset-2026-09-03

Detailed D instructions: https://github.com/satfan52/Thetis/blob/feature/dedicated-processed-tx-output/HYBRID-SDR-BRANCH-D.md

The ci/baseline-build branch is publication tooling, not a product or radio-testing branch.
