# Hybrid SDR — Branch H2 (independent zoom + tag-fit auto zoom)

H2 is the zoom work approved on 2026-11-22: the normal-view and full-window zooms are
independent, the full-window cells get a new 1–64 zoom scale, and every receiver can be set to
AUTO so the RX/TX tag window is zoomed to stay readable. Built from H1 head `648d5c3`.

## What changed

- **Independent full-window zoom.** A full-window cell has its own 1× base span, taken from its
  DDC analyser (sample rate/FFT), not from the normal-view zoom. The renderer, scales, tuning
  windows, click/drag/wheel mapping and the cell zoom sliders all use that base. The normal view
  keeps its own analyser zoom; entering/leaving the full-window view restores it.
- **1–64 full-window scale.** The DDC1–DDC8 cell zoom sliders now run a 1×..64× scale (the old
  scale was 1×..16× layered on the normal-view zoom). 64× is the top of the H2 scale; see
  “Limits” below for what it maps to optically.
- **Per-DDC zoom floor.** The H1-cell analysers get their own zoom limit (150 instead of the
  historical 100) while the full-window view is up. That is what lets the auto zoom fit a 50 Hz
  CW filter at 384 k without changing the FFT configuration. Every non-H1 analyser keeps 100.
- **Tag-fit AUTO.** No extra AUTO buttons anywhere. In the normal view a single `Auto Zoom` pill in
  the zoom row (same skin art and format as the 0.5x/1x/2x/4x presets, dark when inactive, blue
  while active, positioned directly left of the zoom slider with the zoom row shifted 15 px right
  so it is clear of the separate MFJ998R/PA block) flips the normal-view auto zoom; in
  the full-window cells the ZOOM caption/value and the zoom slider itself are clickable to flip
  that DDC between manual and AUTO (a drag on the slider still tunes manually). The target span is
  computed in absolute hertz from
  the window's filter width, the pane/cell pixel width and the measured 7 pt bold TX/RX tag width
  plus an 8 px margin. Full-window auto additionally centres the pane on the tagged window (TX tag
  first, then the RX/audio tag) by setting the analyser pan; the normal view uses the one global
  zoom and picks the most-zoomed requirement of the displayed main windows. Moving the zoom slider
  (or a preset button) by hand leaves AUTO immediately. The AUTO state, normal-view zoom and
  per-DDC zoom are persisted.
- **Sample-rate/FFT aware.** Auto target spans are absolute Hz; the zoom slider value is derived
  from each analyser's current sample rate and FFT size. A rate or FFT-size change re-publishes the
  cell base spans and re-applies the cell auto zoom.
- **No FFT/waterfall rework.** Zooming changes only the analyser span/pan and the published
  extents; `FFTSize`, `SampleRate`, `Decimation`, `Pixels`, window, averaging and the waterfall
  processing are untouched. The existing definition is preserved for the same displayed span.

## Verification (2026-11-22, station)

Deployed `Thetis.exe` `BDCAE3A8…`, build from this branch; normal and full-window views checked on
the running console.

- **Independence:** at the same cell zoom 1×, the DDC1 base span was 34,258 Hz with the normal view
  at 1× and also with it at 4×; entering the full-window view no longer scales the cells by the
  normal zoom. Leaving the full-window view restored the normal 4× axis unchanged.
- **New scale/limits:** DDC1 manual max = 64× (physical 29.09× the cell 1× span, span 1.18 kHz);
  normal view AUTO at 50 Hz CWU selected slider 233 (≈9.4×, span ≈3.75 kHz); DDC1 AUTO selected
  target 2,286 Hz (physical 14.99×, 52× label) and the 50 Hz window/tags stayed readable in the
  cell.
- **Sample rate:** the same 50 Hz/CWU setup produced base span 8,564 Hz at 48 k (DDC1 auto physical
  3.75×, normal AUTO factor 11.76/slider 199), 34,258 Hz at 192 k (physical 14.99×, slider 233),
  and 68,518 Hz at 384 k (DDC1 auto hit the cell floor: physical 29.09×, 64× label; the tag window
  was still ≈20 px). The FFT stayed 131072 and decimation 1 throughout.
- **Persistence:** with normal AUTO and DDC1 AUTO enabled, the console saved
  `autoZoomNormal=1`, `cell0=14.9879,1`, restarted, applied the normal AUTO after the window was
  shown, and re-applied DDC1 AUTO on entering the full-window view; the console started normally.
- **Manual takeover:** clicking the normal 1× preset while AUTO was on logged
  `auto zoom: normal off (manual zoom)` and left the requested zoom in place.
- Console responsive after all tests, no crash dumps.


## Normal-view rendering fixes (2026-11-22, after the first H2 test build)

These two are normal-view rendering defects found on the station after the first H2 test build;
they are independent of the zoom work (same symptoms on the pre-H2 build), and are fixed here
because they were found while testing H2.

- **RX1 frequency readout's first digit clipped.** The docked VFO meter draws the frequency line
  right-aligned at 41.5 % of its widget. With a two-digit MHz part (e.g. `14.086`) the line was
  wider than the space to the left of that anchor, so the render target clipped the leading digit.
  The line is now fitted into the available space (shrink only) for VFO A and VFO B
  (`MeterManager.cs`, the `plotText(sVfoA/sVfoB, ...)` calls now pass a `fitSizeWidth` of
  `0.415 * widget width` with `only_shrink`).
- **dB-scale hover highlight covered the top-left frequency figure.** The translucent
  number-scale highlight was painted after the frequency/band-edge labels. The wash is now painted
  after the grid lines but before the scale numbers and band-edge/frequency callouts, so hovering
  the scale still highlights it but cannot obscure the frequency figure in the top-left corner
  (`display.cs`). The bright number colour switch stays in the label block.

Verified on the station with the fixed build (`Thetis.exe` `2f1e40e0...`): at 14.086842 MHz the
VFO A readout shows the complete `14.086 842` (first digit no longer clipped); hovering the dBm
scale leaves the red `14.000` band-edge/frequency figure at the top-left readable while the
highlight wash is still visible (pixel diff confirms the wash is drawn).


## AUTO control iteration (2026-11-22, final)

- **Normal view:** the plain `Zoom:` label toggle was replaced by a skinned `RadioButtonTS` pill
  labelled `Auto Zoom` in the zoom row. It copies the preset buttons' `Appearance=Button`,
  `FlatStyle=Flat`, font and anchor, is 76x24 (the two-word label needs more width than `1x`) and
  uses the 1x preset's skin art via a mapping in `Skin.cs` (`radAutoZoomNormal` ->
  `radDisplayZoom1x`). The 1x art's 1 px light bottom/right border is clipped away with a
  `Region` (75x23) so the pill draws only its rounded body - no stray line below or to its right. It is repositioned at runtime to sit 2 px left of the live zoom slider; the
  zoom slider/ZTB/preset row is shifted 15 px right at startup so the pill no longer overlaps or
  touches the MFJ998R/PA block (there is a clear gap between the MFJ line and the pill). Inactive
  it is the normal dark button;
  clicking it flips the normal-view tag-fit AUTO and it turns checked-blue while active. There is
  no separate pill anymore.
- **Full window, same language:** no separate pills in the strips. Every full-window cell's ZOOM
  caption (and its zoom value) is clickable to toggle AUTO, and a plain click on the tuning-range
  slider itself toggles AUTO as well; the caption reads `AUTO` in amber while active. Dragging the
  slider tunes manually and leaves AUTO (the existing manual-takeover path).

Verified on the station with `Thetis.exe` `b7087c8a...`: the zoom row shows a clean `Auto Zoom`
pill immediately left of the zoom slider (2 px gap), with a clear gap to the MFJ998R line (the row
was moved 15 px right) and no bottom/right border lines; inactive it is dark with white text,
clicking it makes it checked-blue and enables auto, clicking again returns it to dark/manual
(`auto zoom: normal off (AUTO button)`). Full-window caption/slider toggles still work, and the
earlier VFO-first-digit and dB-scale-highlight fixes remain in place.


## SubRX default separation (2026-11-22)

The sub receivers used to start exactly on their receiver's frequency: a sub that had never been
tuned, a cleared sub row, a sub snapped back into range, or an old saved sub whose frequency sat on
the main one all produced two tuning windows drawn on top of each other. The defaults are now
filter-aware and only apply to a sub enabled for listening (the SPLIT-only TX path keeps its old
TX=RX default, per the agreed Option A):

- Base separation is 5 kHz. The needed separation is the larger of 5 kHz and the distance between
  the two drawn filter windows plus a 100 Hz guard:
  - upper side: `mainFilterHigh - subFilterLow + 100`,
  - lower side: `subFilterHigh - mainFilterLow + 100`.
  So ordinary 2.4-2.8 kHz filters start 5 kHz apart, and wider filters start farther apart so the
  two windows cannot overlap.
- The code tries the upper side first, then the lower side, and falls back to the largest usable
  separation the receiver's 92 % sub passband still allows. It uses the same `SubPositionUsable`
  rule as the existing clamps.
- Applied at: first enable with no stored sub, first enable with an old saved sub exactly on the
  receiver (legacy default), enable with a stored sub whose windows overlap, empty/zero sub row
  commits, the out-of-span/out-of-oscillator snaps, and the startup restore pass (which otherwise
  re-applied the old equal value after the enable repair).
- A valid stored sub that does not overlap is left untouched; the new position is saved through the
  existing `saved_vfoa_sub_freq` / `saved_vfob_sub_freq` and band-memory paths.
- Both normal and full-window views use the same `Display.VFOASub`/`VFOBSub` frequencies, so no
  renderer change was needed.

Tested on the 22 November H2 build (`Thetis.exe` `73fc2fd6...`): with a database where
`VFOBSubFreq` was deliberately set equal to `VFOBFreq` (`1.896456`), the startup restore logged
`sub2 overlapping stored value -> default 1.901456`; the sub was placed exactly 5 kHz above VFO B
with the main/sub filters at -2800/-100 Hz. The SubVFOA handler was also observed logging
`default next to VFOA` (14.082442 from VFOA 14.077442) for an out-of-span sub. The Option A guards
are in the RX1 `VFOAUpdate` snap and the empty/zero SubVFOA commit (split-only keeps VFOA), and the
RX2/SubRX2 paths only use the default while `chkEnableMultiRX2` is on.



## VFO first-digit overlay fix (2026-11-22, after the sub-default build)

The leading digit of the main VFO A/B readout was partially covered by a **35x11 empty
`LabelTS` over the readout**: `lblRX1APF` at `grpVFOA (45,20)` (and `lblRX2APF` at the VFO B
twin). These labels are shown by the CW mode-change code via `lblRX1APF.Show()`, but their text is
never set anywhere in the fork - they stay empty. A transparent WinForms label paints its parent's
background over its rectangle instead of the sibling textbox underneath, so the empty label erased
the left edge of the first digit (the same failure appeared at 14 MHz and had nothing to do with
the font or the sub-row defaults).

Fix: never leave an empty APF label visible. The labels are hooked with `VisibleChanged`, which
hides them again whenever they are shown with empty text, and the `RX1APFlabel`/`RX2APFlabel`
setters also hide the empty label. Font sizes and box widths are unchanged (an earlier font/width
experiment was discarded on the user's instruction).

Verified with `Thetis.exe` `16af547a...`: after the fix the enumeration around the first digit
shows no visible STATIC overlay, and the readout shows the complete `14.077 442`; hiding the
35x11 empty STATIC by hand reproduced the fix immediately. RX2's `lblRX2APF` uses the same handler.



## Cell AUTO exit restores the manual zoom (2026-11-22)

Enabling a full-window cell's AUTO zoom changed `Display.H1CellZoom` and the cell analyser, but
leaving AUTO by its ZOOM caption only cleared the AUTO flag: the auto-applied zoom and pan stayed
in force, the cell slider was left at the auto value, and the operator's previous manual scale was
lost. In the reported case the scale stayed at the auto position and could not be brought back to
the no-zoom state.

- `H1AutoZoomCellApply` now remembers the cell's manual zoom the first time AUTO takes it
  (`_h1AutoZoomSaved[8]`).
- Turning AUTO off by clicking the ZOOM caption calls `H1AutoRestoreCellManualZoom`: it puts the
  remembered manual zoom back, syncs the slider/value label, clears the stale auto pan by applying
  the normal analyser path, and invalidates the pane.
- A manual slider move while AUTO is on still takes over immediately and clears the remembered
  value; re-entering AUTO captures the new manual zoom.

Tested on DDC1 in full-window mode with `Thetis.exe` `9afdc0c3...`: manual 64x → AUTO (auto target
52x) → AUTO off returned the cell to **64x** (`cell zoom: DDC1 physical 29.09 ...`; trace
`auto zoom: DDC1 off (zoom caption)`), and a following manual drag returned it to 1.0x. Before the
fix the AUTO-off value stayed at the auto position.



## Full-window 1x span fix (2026-11-22)

DDC1 in full-window mode at manual 1x only showed about **34 kHz** at a 192 kHz sample rate,
while normal mode showed the receiver's wide span. The cell base span had been built from the
analyser's **normal-view 1x zoom** (`GetFrequencyExtents(z1, 0.5)` with z1 = 0.6087), which cuts
the usable bandwidth down to about one fifth; that was the wrong reference for the full-window
cells.

- Full-window 1x is now the cell's **whole usable DDC bandwidth**: the analyser extents at z=0
  (`H1OneXSliderValue()` returns 0 for the cells). At 192 k the DDC1 base span is
  **176,638 Hz** (the analyser's 92 % usable area) instead of 34,258 Hz.
- The cell slider's 1x..64x scale now runs from that full-band base down to the H1 definition floor
  (`H1CellPhysicalMaxZoom()` = 150 optical), so manual 1x shows the whole band and auto zoom has a
  full-band reference for its tag-fit target.
- The normal view's zoom mapping is untouched; this only changes the per-cell base in full-window.

Verified on DDC1: manual 1x now shows the full 14 MHz band (frequency labels 13.998..14.158 MHz),
with `cell zoom: DDC1 base span -88319..88319 (176638 Hz, rate 192000, fft 131072)`. The AUTO
on/off restore was re-tested on all eight DDCs: manual 64x -> AUTO -> off returned 64x, then 1x ->
AUTO -> off returned 1x on every cell (DDC1 auto value 56x with the 50 Hz filter; DDC2-8 5.5x
with the 2.7 kHz filters).



## CTUN click/wheel target correction (2026-11-22)

The current receiver model is VFOA = RX1, SubVFOA = SubRX1, VFOB = RX2,
SubVFOB = SubRX2. The window-drag handlers already followed that model, but the
CTUN click-tune and mouse-wheel target selection still had the old "VFOB is RX1's
second VFO" assumption:

```csharp
if (current_click_tune_mode == ClickTuneMode.VFOB &&
    (chkVFOSplit.Checked || chkEnableMultiRX.Checked))
    VFOASubFreq = clickedFrequency;   // SubRX1
```

This was reproduced on the station: with RX2 + SubRX1 + SPLIT on and the CTUN
crosshair in red VFOB mode, a left-click in the blank RX2 pane ran
`ctun left: SPECIAL -> VFOASubFreq=1.896564`; VFOB stayed unchanged and SubVFOA
moved. The same legacy redirect existed in the normal-view wheel cases
(`TuneLocation.DisplayTop` / `DisplayBottom`) and in the full-window DDC1/DDC2
caption wheel, prefill and manual-entry helpers.

The target is now chosen by the clicked display half, matching the drags:

- RX1 pane -> `VFOAFreq`
- RX2 pane -> `VFOBFreq`
- SubRX1 window -> `VFOASubFreq` (its priority hit-test already handles this)
- SubRX2 window -> `VFOBSubFreq` (same)

The SPLIT/MultiRX redirect to `VFOASubFreq` was removed from CTUN click and from
the normal/full-window wheel paths. The dual-VFO wheel fallback is kept only for
the RX2-off layout. SubRX frequencies are therefore reachable by their windows,
not by a VFOB-mode click or wheel elsewhere.

Verified with the corrected build:

- Red VFOB mode, blank **RX2** pane click: `VFOB` went 1.896456 -> 1.896564;
  SubVFOA stayed 14.076380.
- Red VFOB mode, blank **RX1** pane click: `VFOA` went 14.077442 -> 14.079381;
  VFOB and SubVFOA stayed unchanged.
- Wheel over the RX2 pane: `VFOB` 1.896564 -> 1.896560.
- Wheel over the SubRX1 window: `SubVFOA` 14.076380 -> 14.076370 (sub-window
  hit test still owns it).
- Station frequencies restored afterwards; CTUN and click-tune mode left off.



## Yellow/blue click-tune target windows (2026-11-22)

The click-tune crosshair now selects the **class of tuning window**:

| Crosshair | Target class | Clicked RX1 pane | Clicked RX2 pane |
|---|---|---|---|
| Yellow | main receivers | VFOA (RX1) | VFOB (RX2) |
| Blue | sub receivers | VFOASub (SubRX1) | VFOBSub (SubRX2) |

Right-click cycles `Off -> yellow -> blue -> Off`. The red VFOB colour and its
legacy split/multirx reset rules are gone. Yellow uses `m_bDX2_Yellow`; blue is a
new `m_bDX2_Blue` Direct2D brush.

Blue targets are only applied when the corresponding sub receiver exists
(`chkEnableMultiRX` for SubRX1, RX2 plus `chkEnableMultiRX2` for SubRX2); otherwise
the click does not move a hidden sub frequency. Direct SubRX1/SubRX2 window
dragging still tunes those subs as before.

Verified with SubRX1 and SubRX2 enabled, SPLIT off, both CTUN buttons on:

- Yellow, blank RX1 pane click: VFOA 14.077442 -> 14.079381; SubVFOA unchanged.
- Yellow, blank RX2 pane click: VFOB 1.896456 -> 1.896564; SubVFOB unchanged.
- Blue, blank RX1 pane click: SubVFOA 14.076380 -> 14.081320; VFOA unchanged.
- Blue, blank RX2 pane click: SubVFOB 1.885964 -> 1.896672; VFOB unchanged.
- Right-click cycle sampled as yellow -> blue -> Off; crosshair colours confirmed
  in the captures.
- Station frequencies restored afterwards; CTUN and click-tune mode left off.



## Full-window per-DDC yellow/blue targets (2026-11-22)

The yellow/blue click-tune target selection now applies to all eight DDC cells in
the 8-DDC full-window view:

- DDC1/RX1 and DDC2/RX2 (the two left-column receiver cells) use a simple-click
  tracker: yellow -> VFOA/VFOB, blue -> SubVFOA/SubVFOB when the corresponding sub
  is enabled. Filter and window drags are left to the normal full-window gesture
  path.
- DDC3..DDC8 start their tuning through `H1SpareCellMouseDown`. Yellow now targets
  that cell's main `HeadlessSlice`; blue targets its `HeadlessSubRX`. The top
  caption strip still pans the DDC, and blue filter-edge drags now belong to the
  sub window instead of the main window.
- A right-click on any of the eight cells now reaches the global colour cycle
  (`Off -> yellow -> blue -> Off`); previously DDC3..DDC8 consumed right-clicks.
- The cell frequency mapping for DDC1/DDC2 falls back to `CentreFrequency` /
  `CentreRX2Frequency` because their row-centre array entries are only populated
  for the headless DDCs.

Verified in full-window with SubRX1/2 and the needed DDC subs enabled:

- DDC1 yellow click: VFOA 14.077442 -> 14.077720; DDC1 blue click: SubVFOA
  14.077998 (main stayed 14.077720).
- DDC2 blue click: SubVFOB 1.942449.
- DDC3 yellow click: main slice 5.357167 -> 5.425062; DDC3 blue click moved its
  sub (trace `click cell rx2 sub -> 5425062`).
- DDC5 blue click moved its sub (trace `click cell rx4 sub -> 18064489`); main
  caption stayed 18.062833.
- Right-click on the DDC3 cell cycled yellow/blue/Off in the captures.
- VFOA/SubVFOA/SubVFOB restored; DDC3/DDC5 SUB toggles returned to off. The
  DDC3/DDC5 headless test frequencies are transient cell state.



## Full-window crosshair rendering (2026-11-22)

The eight-DDC renderer bypasses the normal panafall drawing path, so the
`CurrentClickTuneMode` crosshair was never drawn in full-window mode. The
click-tune modes did change colour/state internally, but there was no visible
crosshair in a DDC cell.

- `H1DrawEightRowsDX2D` now draws a cell-local yellow (main) or blue (sub)
  crosshair at the pointer, clipped to the cell under the cursor. The horizontal
  line follows `ShowCTHLine`.
- `pnlDisplay_MouseMove` updates `DisplayCursorX/Y` for full-window cells too; the
  spare cells suppress their cursor-info text, which had previously left the
  coordinates stale.
- Right-clicking a cell's caption row now reaches the global colour cycle; the
  caption guard only consumes left presses.
- One remaining normal-view crosshair call site was still using the old
  grid/red brush pair; it now uses yellow/blue like the rest.

Verified in full-window: right-click on the DDC3 spectrum drew a yellow vertical
crosshair, a second right-click changed it to blue, and a third removed it.


## Full-window DDC1/DDC2 CTUN click mapping (2026-11-22)

The full-window yellow click on DDC1/DDC2 with CTUN on moved the tuning windows to
the wrong frequency. The cell renderer plots around the hardware/display centre
Display.CentreFreqRX1 / CentreFreqRX2, but the click mapper used the console's
CentreFrequency, which under CTUN is the frozen display centre. The mapper now
uses the same Display.CentreFreqRX1/RX2 values as the renderer. Verified with DDC1
CTUN on: clicking at the same pixel produced VFOA 14.078320, and the RX1 tuning
window landed under the clicked pixel; blue still moved SubVFOA with the main
caption unchanged.


## Limits

- The 1–64 scale is the H2 full-window scale: 64× is the deepest span the H1 cell floor allows
  (the analyser floor is `zoom_limit` of the usable FFT span). It is not a literal 64× multiplier
  of the normal-view 1× span; that would need a deeper analyser floor and stretch the existing FFT
  bins, which was rejected to keep the waterfall definition as it is today.
- At 384 k with a 50 Hz filter the cell auto zoom lands at the floor; narrower filters or higher
  sample rates would need a tag fallback or a deeper floor (not implemented here). The normal view
  has the same physics; the cell floor was raised to 150 specifically to cover 50 Hz at 384 k.
- DDC1/DDC2 cell zoom and auto flags are saved as extra `H1Slices` keys (`cell0`, `cell1`); old
  `ver/3` slice records load their cell zoom at the neutral 1× (the old scale cannot be mapped
  reliably).

## Files

- `Project Files/Source/Console/HPSDR/specHPSDR.cs` — per-analyser `ZoomLimit`.
- `Project Files/Source/Console/display.cs` — per-row base spans, span publication, renderer base.
- `Project Files/Source/Console/console.cs` — independent cell zoom, AUTO logic/UI, persistence,
  sample-rate/FFT base refresh, diagnostics.
