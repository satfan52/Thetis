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
- **Tag-fit AUTO.** An AUTO pill in the normal view and in every full-window cell strip
  (`specHPSDR.ZoomToBandwidth`-style target). The target span is computed in absolute hertz from
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
