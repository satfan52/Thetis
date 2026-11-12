# Hybrid SDR Branch H1 — Eight-DDC Full-Window Receivers and the Hybrid Station Console

Branch H1 is the **development line** for the hybrid station built in this fork: a Red Pitaya
running Pavel Demin's receiver-only 8-DDC HPSDR firmware as the receiver chain, with an Icom IC-7100
as the only RF source. It builds on Branch G's headless multi-port TCI server and adds the
console-side hybrid work: RX1/RX2 parity, the two sub receivers, the 2x4 full-window view in which
every DDC is a full receiver, spare-slice listening audio, single-source transmit with CI-V keying,
per-slice persistence, the OM2000A+ LAN amplifier integration, IC-7100 rig control and meters,
per-slot CAT protocol, four exclusive transmit ticks and console layout v5.

H1 is **experimental**. The mature test bed is `build/stable-617-619` — deployed to `C:\Thetis`, it is
what the station runs and what feeds the upstream PRs. A change validated on the stable line is
ported to H1; H1 features are not back-ported unless asked for.

---

## 1. Station architecture — the constraints every change lives under

| Fact | Consequence |
|---|---|
| Red Pitaya runs Pavel Demin's *receiver-only* HPSDR firmware (8 DDCs, Protocol 1) | No transmitter, no real mic audio; the TX DSP is still clocked because the Protocol 1 frames carry mic-sample slots |
| All RF comes from the IC-7100 (Processed TX Output audio, keyed over CI-V) | The PC has **no observable for RF or TX audio**; only the operator confirms on-air behaviour |
| DDC mapping: RX1→DDC0, RX2→DDC3, RX3→DDC1, RX4→DDC2, RX5→DDC4, RX6→DDC5, RX7→DDC6, RX8→DDC7 | `HeadlessSliceManager.RxToDdc`; the GUI cells and the TCI servers address the same slices |
| TCI: port 50001 = full two-TRX server (RX1/RX2); ports 50002–50008 = one headless receiver each (RX2–RX8, port = 50000 + RX number) | One receiver per headless port; RX2 is reachable both ways |
| VAC1/VAC2 are the audible path on this station; each WDSP channel has panel gain | Spare-slice audio goes through the native audio mixer (section 3) |

## 2. The 2x4 full-window view — every DDC is a full receiver

The view splits the display into two columns of four rows: RX1 and RX2 in the left top two cells, then
RX3–RX8 in the remaining six.

| Row | Receiver | DDC | WDSP channel pair |
|---|---|---|---|
| 0 | RX1 | DDC0 | 0 / sub 1 |
| 1 | RX2 | DDC3 | 2 / sub 3 |
| 2 | RX3 | DDC1 | 4 / sub 5 |
| 3 | RX4 | DDC2 | 6 / sub 7 |
| 4 | RX5 | DDC4 | 8 / sub 9 |
| 5 | RX6 | DDC5 | 10 / sub 11 |
| 6 | RX7 | DDC6 | 12 / sub 13 |
| 7 | RX8 | DDC7 | 14 / sub 15 |

Each cell draws its own spectrum and waterfall (analysers 0..7) and carries:

- **One control strip** (688 x 20, a single row over the top of the cell): band, mode, AGC, AF, CTUN,
  NR/ANF/NB/BIN and ZOOM. The strips are built per cell (`H1Fs*`, `h1cellStrips`); inapplicable
  controls stay visible.
- **A live S-meter**, horizontal with S0..S9+60 scaling, computed from WDSP directly
  (`H1PublishCellMeters`, 150 ms) rather than the legacy `new_meter_data`, which freezes while the
  console meters are hidden.
- **Tuning windows** — the VFO window and the sub window, with wheel tuning, body drag, filter-edge
  drags and a band menu. Every hit test uses the per-cell drawn bounds (`H1WindowUnder` records all
  eight cells), so the gestures act on the cell under the pointer.
- **A "TCI" badge** in the top-right corner when a TCI client uses that slice (`Display.H1RowTci`,
  from `SliceConsumer.TciAudio` / `TciIq`).
- **A SUB toggle**: RX1/RX2 drive the console's multi-RX ticks; RX3..RX8 enable their headless sub
  receiver through `HeadlessSubRX`.
- **A per-cell ZOOM** that zooms that DDC's own analyser (`H1ApplyCellAnalyserZoom`,
  `H1ApplyCellZoom`). Analysers 2..7 publish their span (`Display.H1StoreCellSpan`) and the renderer
  accepts a published span for every row, so a zoomed cell shows real FFT detail instead of cropped
  plateaus. Each row's zoom is re-applied when the view opens.

The normal-view control groups are **hidden** while the full-window view is up
(`H1InitNormalPanelGuard`, `H1HideNormalViewPanels`, `H1RestoreNormalViewPanels`; a `VisibleChanged`
veto re-hides anything that tries to re-show). Their exact visibility is restored on exit.

**Persistence** — `H1Slices` ver/3, written at power-down and on exit and restored after `GetState`.
For RX3..RX8 it stores a positioned flag, frequency, mode, filter low/high, AGC top, AF gain,
NR/ANF/NB/BIN, the CTUN model, sub enabled/frequency/filter and cell zoom. Only a receiver the
operator actually positioned keeps its frequency when the view opens; an untouched cell follows RX1's
centre. Rows from an older layout are ignored.

## 3. Spare-slice audio — listen to any DDC

- **Middle-click a cell's VFO window** toggles that receiver's audio; **middle-click its sub window**
  toggles the sub. The green listen pills of the first design were removed.
- The console mixes the slice as aamix input `2*rx` (`2*rx+1` for its sub), and because the audible
  path on this station is VAC, the native mixer routes a spare receiver's audio into VAC1/VAC2:
  `SetSpareVACRoute(rx, mask)` in `Project Files/Source/ChannelMaster/pipe.c` (bit0 VAC1, bit1 VAC2,
  bit2 sub-only; state in `spare_vac_route` / `spare_vac_takeover`).
- **The selected spare takes over that VAC's receiver input**, and RX1/RX2's own feed pauses — two
  writers on one mixer input interleave two 48 kHz streams into noise. One spare is the listening
  source at a time; VAC1 is switched on when the first spare is listened to and off with the last; a
  sub-only selection routes the sub channel alone instead of the main+sub mix.
- Symbols: `H1ToggleSpareAudio`, `H1SetSpareAudio`, `H1SpareSliceHasAudio`,
  `H1ReapplySpareAudioStates`, `H1ClearSpareAudio` (on view exit).

## 4. Transmit on a spare slice — exactly one TX source

- **Shift+middle-click** in a spare cell selects that slice as the voice-TX source; shift+middle-click
  its sub window selects the sub VFO (`H1ToggleVoiceTxSlice(row, sub)`).
- The cell shows the **standard RX1/RX2 TX tag and edges** inside the window (`Display.H1RowTxMask`,
  bit0 main / bit1 sub, drawn through `H1DrawCellWindow`) — not a separate badge.
- Selecting a spare clears the RX1/RX2 transmit ticks (their tag disappears); choosing an RX1/RX2
  tick again drops the spare; the previous tick is restored when the spare is cleared or the view is
  left (`_h1VoiceTxSavedTicks`, `H1TxTicksMask` / `H1SetTxTicks` / `H1TxTickChanged`).
- Console **PTT** (`H1VoiceTxMoxChanged` on `MoxChangeHandlers`) steers and keys the IC-7100 through
  CI-V on that slice's frequency and mode (`CIVController.SteerAndKeyForDigitalTx`), sets the TX DSP
  mode to the slice's mode, and restores the rig and the DSP mode on release (`ReleaseDigitalTx`,
  `_h1VoiceTxKeyed`, `_h1VoiceTxSavedMode`). The TUN path is untouched.
- The transmit VFO is one of **four exclusive ticks** (VFO A, VFO B, SubVFOA, SubVFOB) — see
  section 5.

## 5. Console parity, sub receivers, four transmit ticks, layout v5

These landed before the full-window work and are part of H1:

- **RX1/RX2 parity**: RX2 has its own band block and stack, AF/AGC/filter/noise/squelch controls and
  the same gestures as RX1; one receiver's controls never drive the other receiver.
- **SubRX1 and SubRX2** (`HeadlessSubRX`; RX1's sub is the console's multi-RX sub, RX2's is its own):
  their own control strips (mode, filter width and shift with Low/High values and Reset, AGC gain and
  profile, noise toggles that step the whole NR set), their own tuning windows with wheel, body drag
  and filter-edge drags, their own S-meters, and band memory / restart persistence.
- **Four exclusive transmit ticks** for VFO A, VFO B, SubVFOA and SubVFOB. SPLIT follows them (arming
  any tick other than VFO A engages split; pressing SPLIT arms SubVFOA), the transmit frequency and
  the CI-V layer follow the armed tick, and the sub ticks present exactly like the main ones.
- **IC-7100 VFO B rule**: Thetis VFO B reaches the rig only when the VFO B tick carries the transmit
  frequency; with RX2 on and no transmit role, VFO B stays off the rig entirely.
- **Split and PTT hardening**: the split sequence releases split before addressing an IC-7100 VFO,
  reads both rig VFOs back, and a VFO B write aborts when the rig never confirms the VFO B select.
  Every commanded PTT state arms a verification poll and is resent at most twice; reports that
  contradict a fresh command inside 400 ms no longer flip MOX. Traces: `civ_split.log`, `civ_ptt.log`.
- **Layout v5**: 156 px side strips with the RX1 controls left of the panadapter and RX2 mirrored
  right, the band blocks and audio groups below the panadapter, the six meters in the top row, the
  VFO and TRANSMIT clusters centred, and the off-state dim veil / RX2-off greying with a one-second
  watchdog.
- **Tune paths**: two-phase tune (antenna tune, then operating tune), the Smart Tune option renamed
  *Two-step tune*, the labels *PA Drive* / *ANT Tune*, and the band-change guard that bypasses the
  amplifier and runs at the ANT Tune level until the SWR settles.

## 6. Amplifier: OM2000A+ over LAN

`Project Files/Source/Console/CAT/AmpLanController.cs` controls the OM2000A+ over the network with
the protocol recovered from the official manager and bench-verified: `C` handshake (each press
toggles the client slot), `?` status, `1` info, 1 s `T` poll, `O` toggle confirmed by state frames,
one retry; the link is held only while an option is on because the amplifier accepts a single client.
The console block carries the PA power pill, Auto tune with remote stop, the activity readout and the
FWD / REF / SWR meters decoded from the amplifier's own binary meter frames. During TUNE the
amplifier is switched to stand-by and returned to operate when tune ends; a band change bypasses the
amplifier. Trace: `amp_lan.log`. The MFJ998R relay controller (`MfjRelayController`) drives the
external ATU with the same two-phase tune.

## 7. IC-7100 rig control and per-slot CAT

- **Per-slot CAT protocol**: any of the four CAT ports can speak Icom CI-V or Kenwood TS-2000
  (`CIVController` / `CIVProtocol`; the CI-V options live on the Transceivers page). Each slot has
  its own CI-V address and a master tick; the IC-7100 options and the rig duties follow the marked
  master.
- **Rig power**: the active power slider (Drive, Tune, 2-Tone) is the single owner and writes the
  IC-7100 RF power with command `14 0A` (big-endian BCD 0000-0255); the level is pinned at TUNE,
  2-tone, power-up and link-open, and banked at power-down.
- **Rig meters**: while transmitting, the rig's forward power and SWR (`15 11`, `15 12`) are polled
  at 5 Hz and can supply the SWR / FWD / REF transmit readings (REF derived from the
  forward/reflected ratio); stale or closed-rig data falls back to the Red Pitaya sensing. Trace:
  `civ_meters.log`.

## 8. Headless TCI additions (on top of Branch G)

- Ports 50002–50008 serve RX2–RX8, one receiver per port; port 50001 keeps the full two-TRX server.
- **Policy A — shared control**: the GUI cells and the TCI servers are front-ends to the **same**
  `HeadlessSliceManager` slices. A slice is reference-counted with `SliceConsumer` (`Display`,
  `GuiAudio`, `TciAudio`, `TciIq`) and is only switched off when its last consumer goes. The GUI and
  TCI cannot be given independent tuning — there are no free DDCs.
- H1 additions on the headless ports include `subrx:0,<bool>` (the port's own WDSP sub channel),
  `vfo:1,0,<hz>` (VFO B tuning) and the `HeadlessSubRX` state; per-port handlers act on the receiving
  server's own `BaseRxIndex`.
- Echo-only stubs and the commands that remain unavailable on headless ports are as in Branch G (the
  port-50001-only set), unchanged by H1.

## 9. Verification status

- **Operator-confirmed on 2026-11-12** (deployed `Thetis.exe` `2C1EE62E…`, built from commit
  `addd8fb`): spare-cell audio is clean, the SUB toggles work, exactly one TX tag shows, and a spare
  slice selected with shift+middle-click keys the IC-7100 on that slice's frequency and mode and
  restores the rig on release. The operator confirmed transmitting on air on the IC-7100.
- **Earlier operator confirmations** carried in this line: RX2/SubRX2 audio on the VACs, the sub
  tuning windows, wheel/drag/filter-edge gestures, the four transmit ticks and split behaviour, and
  MiniTCI PTT/TUNE on a headless port (2026-09-14, VAC1 disabled).
- **Only the operator can confirm on-air behaviour** — the PC has no RF or TX-audio observable.
- **Not verified**: CPU load with all eight DSP channels and analysers streaming at once (the manager
  keeps slices dormant until a consumer arrives); mixed per-DDC sample rates on the 8-DDC firmware;
  load-testing GUI and TCI consumers on the same spare. The Red Pitaya is the only validated radio —
  this is not a general HPSDR compatibility claim.

## 10. Build, deploy and rollback

Build the full solution (managed + native) from `Project Files\Source`:

    "C:\Program Files\Microsoft Visual Studio\18\Community\MSBuild\Current\Bin\amd64\MSBuild.exe" Thetis_VS2026.sln -p:Configuration=Release -p:Platform=x64 -m -v:m -nologo

For a console-only change, `Console\Thetis.csproj` with `-p:BuildProjectReferences=false` is enough.
The managed output is `Project Files\bin\x64\Release\Thetis.exe`; the native ChannelMaster output is
`Project Files\Source\bin\x64\Release\ChannelMaster.dll` (the MSB8012 TargetPath warning is benign).

Deploy: kill Thetis and MiniTCI first, bank the outgoing binaries to
`C:\Thetis\rollbacks\Thetis_<sha16>.exe` and `ChannelMaster_<sha16>.dll`, copy the new ones, launch
exactly one instance from `C:\Thetis`, and verify the deployed hashes. When switching between the
stable line and H1, deploy the full native set (`Thetis.exe`, `ChannelMaster.dll`, `wdsp.dll`,
`PA19.dll`, `cmASIO.dll`) — a mixed managed/native set is exactly the untested-binary hazard.

Station data and traces: database `%APPDATA%\OpenHPSDR\Thetis-x64\DB\<guid>\database.xml` (the
`H1Slices` table is ver/3); logs beside the exe — `space_ptt.log` (H1SpacePttTrace),
`civ_ptt.log`, `civ_split.log`, `civ_meters.log`, `amp_lan.log`, `mfj_relay.log`, `tci_flow.log`.

## 11. Source changes (H1 vs `origin/master` 852bf0e)

545 commits touched 42 files under `Project Files/Source` (+57,275 / -28,231 lines). The main files:

| File | Change |
|---|---|
| `Console/console.cs` | +17,409 / -3,968 - the console: layout, the full-window view, strips, panels, TX ticks, persistence, amplifier block, dim veil |
| `Console/console.resx`, `console.Designer.cs` | +19,425 / -18,989 and +176 / -10 - resources and designer surface for the console work |
| `Console/CAT/CIVController.cs` (new) | +3,882 - Icom CI-V control, rig power and meters, split and PTT verification |
| `Console/Skin.cs` | +1,853 / -1,760 - skin mapping for the new controls |
| `Console/Thetis.csproj` | +1,368 / -1,356 - new source files and references |
| `Console/HeadlessTciServer.cs` (new) | +2,431 - per-port TCI servers on 50002-50008 |
| `Console/HPSDR/specHPSDR.cs` | +1,033 / -981 - analyser 2..7 span publishing |
| `Console/cmaster.cs` | +1,080 / -795 - audio mixer routing, TCI hooks, native interop |
| `Console/setup.cs`, `setup.designer.cs` | +1,518 / -2 and +336 / -56 - Transceivers page, amplifier options, TX meters |
| `Console/CAT/AmpLanController.cs` (new) | +1,499 - OM2000A+ LAN control |
| `Console/TCIServer.cs` | +1,217 / -167 - port 50001 server (Branch G lineage plus H1 changes) |
| `Console/display.cs` | +1,142 / -20 - eight-row renderer, per-cell windows, meters and zoom |
| `Console/HeadlessSliceManager.cs` (new) | +770 - shared slices, reference-counted consumers |
| `Console/CAT/CIVProtocol.cs` (new) | +510 - CI-V frame codec |
| `Console/CAT/MfjRelayController.cs` (new) | +429 - MFJ998R ATU |
| `Console/TxArbiter.cs` (new) | +337 - single active digital-TX slice |
| `Console/HeadlessSubRX.cs` (new) | +261 - per-slice sub receiver |
| `Console/audio.cs` | +157 - spare-slice aamix inputs |
| `Console/clsBandStackManager.cs`, `database.cs` | +43 and +34 / -1 - band stack and DB support |
| `ChannelMaster/pipe.c`, `cmaster.c`, `ivac.c`, `networkproto1.c` (+headers) | native mixer and VAC routing, `SetSpareVACRoute`, 8-DDC rate handling |
| `TestScripts/MiniTCI.py` (new, H1-only file) | +4,660 - the reference TCI client, plus the probe and test suites |
`TestScripts/` files are tracked only on H1: a checkout of another branch removes them from disk, so
restore one with `git show H1:TestScripts/<file> > TestScripts/<file>` before running it.

## 12. Known limits and open items

- Spare-cell band combos currently use RX1's band stack (the last-used frequency/mode for that band);
  per-slice band-stack memories are **not** implemented.
- CPU with all eight DSP channels and analysers active at once has not been measured; slices stay
  dormant until a consumer (display, GUI audio, TCI audio or TCI IQ) arrives.
- Database state created by the feature tests can survive in the station DB (spare SUB flags, cell
  zooms); reset it if a clean slate is wanted.
- Headless TCI ports keep their Branch G limits: echo-only stubs for RIT/XIT/squelch/lock and the
  port-50001-only commands; no second TRX per port.
- MiniTCI is not part of the H1 release archive; it lives in `TestScripts/` and `TestScripts/dist/`.

## 13. History notes (do not re-derive)

- **The master-VFO / CTUN rework was attempted and reverted.** A four-commit series redistributed the
  DDC-centre state across server, protocol and client rendering; it made the display worse in both
  modes and was reverted commit by commit. The rule that survived: the display centre has exactly one
  source of truth, and a state-model change must land atomically.
- **Two writers on one VAC mixer input produce noise.** Spare-slice audio is single-producer with
  explicit takeover; do not add a second audio path into the same mixer input.
- **Never share one receiver's controls with another:** a shared slice control makes the other
  receiver silent and meterless. The shared-slice model uses reference-counted consumers, not shared
  UI.
- The full-window control block went through a 194x92 panel, two slim rows and finally one row; the
  one-row strip is the accepted layout.
- Band-stack and sub-frequency fixes have a history of reference bugs: when a clamp is symmetric in
  code and the log proves it fires symmetrically, suspect a missing filter-geometry term rather than a
  wrong reference value.

## 14. Lineage and releases

```
master (852bf0e)
  └── D (dedicated processed TX output)
        └── E (CI-V + processed TX output)
              └── F (multi-port headless TCI server)
                    └── G (SDR-software compatibility: IQ, DDS, S-meter, AGC, mute)
                          └── H1 (hybrid console development line — this branch)
```

Test set releases:

- `hybrid-sdr-testset-H1-2026-11-12` — current: eight-DDC full-window receivers and spare-slice
  audio/transmit, built from commit `addd8fb`.
- `hybrid-sdr-testset-H1-2026-09-26` — the earlier sub-receiver test set (superseded entry).
