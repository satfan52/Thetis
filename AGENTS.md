# Thetis — agent guide (satfan52 fork)

Thetis is an OpenHPSDR Windows SDR console: C# / .NET Framework (WPF + WinForms) under
`Project Files/Source`, native C in `Project Files/Source/ChannelMaster`, Python test clients in
`TestScripts`. This checkout is **satfan52's fork of ramdor/Thetis**, developing the Hybrid SDR
line: TCI, SubRX, CAT/CI-V, processed TX.

## Remotes and branches

| Remote | URL | Use |
|---|---|---|
| `origin` | github.com/ramdor/Thetis | upstream — **never push** |
| `satfan52` | github.com/satfan52/Thetis | your fork — the only push target |

- `H1` — **active development line**, currently checked out in the main worktree.
- `build/stable-617-619` — stable build line.
- `fix/*`, `feature/*` — PR branches against upstream.
- `master` mirrors upstream master.
- Push only to `satfan52`. No force-push, no rebase or amend of already-pushed branches. Do not
  delete remote branches.

Other worktrees exist beside the main one (`Thetis_stable`, `Thetis_pr619`, `Thetis_pr620`,
`Thetis_bisect`, `Thetis_h1_review`, `Thetis_d8986`, `Thetis_PatchVerification\Repo`). Treat the
worktree you were started in as your workspace; do not switch branches in it.

## Build

```
"C:\Program Files\Microsoft Visual Studio\18\Community\MSBuild\Current\Bin\MSBuild.exe" ^
  "Project Files\Source\Thetis_VS2026.sln" -p:Configuration=Release -p:Platform=x64
```

Output: `Project Files\bin\x64\Release\Thetis.exe`.

The repo lives inside OneDrive: expect slow IO, occasional file locks, and `bin/obj` churn. If a
build fails on a locked file, retry after a few seconds before investigating.

## Deploy and verify

Deployment is **the user's step**; ask before doing it.

1. Kill `Thetis` and `MiniTCI` first — the console holds the audio devices, CAT ports and the
   Arduino/USB serial port open.
2. Bank the outgoing binary: copy the current `C:\Thetis\Thetis.exe` to
   `C:\Thetis\rollbacks\Thetis_<sha16>.exe`.
3. Copy the new build to `C:\Thetis\Thetis.exe`, relaunch from `C:\Thetis`.

Verification rules:

- On-air behaviour needs the radio — only the user can confirm it.
- What an agent can verify: a clean MSBuild, log lines, probe scripts (`TestScripts/*.py`,
  `~/.dsh/skills/thetis-hybrid-sdr/scripts/*`), counters, read-back over TCI.
- **Never claim something is verified without a measurement.** "Builds and the probe shows X" is
  acceptable; "should work" is not.
- When a regression appears, bisect it — do not guess.

## Conventions

- Preserve **CRLF** line endings and each file's existing BOM.
- Draft commit messages, get them approved, then commit. Messages describe behaviour and cause,
  not a file list; the existing log is the model.
- Keep a PR's title and body current as its branch grows.
- Uncommitted and untracked files in this repo are **expected** (probe scripts, `*.h1bak*`,
  `*.bak_*`, `.wip_*`). Do not clean, revert, delete or commit them unless explicitly asked.
- Never run: `git reset --hard`, `git clean`, `git checkout -- .`, `git restore .`,
  `git stash drop`, `git branch -D`, `git push --force`.

## Domain notes

- **TCI** — WebSocket control/audio protocol on ports 50001–50008; `TestScripts/MiniTCI.py` is the
  reference client. Rebuild the MiniTCI exe (PyInstaller) after editing its source, or users run
  stale code.
- **SubRX** — per-receiver sub channel; WDSP channel ids follow `WDSP.id(2*rx, 1)`. RX2 keeps its
  own mode and filter per band.
- **CAT / CI-V** — native Icom protocol on CAT 1; addresses stay editable while CAT is open.
- **ChannelMaster** — native C audio/pipe layer feeding the console.
- Audio: WASAPI in VAC1 and TX out; never recommend MME. Prefer network I/Q (HPSDR UDP) and TCI
  over virtual audio cables.
- PTT must be near-instant and event-driven off the control's change event — never polled.

## Working with this user

- Plain answer first (yes/no, or the finding), then the detail. Short, factual, no marketing.
- Reports must be complete — do not drop rows or findings for brevity.
- Diagnosis is agreed before edits; UI labels and tooltips are approved before implementation.
- Prefers concrete options over open questions; a default should never just stand.
- Cost-sensitive: scope tasks, avoid wandering the whole tree, and say when a request will be
  expensive.

## Deeper project knowledge

`~/.dsh/skills/thetis-hybrid-sdr/` — `SKILL.md` (index) plus `references/` (build/deploy/bisect,
TCI architecture, console layout, SubRX, CI-V, ChannelMaster router, station hardware) and
`scripts/` (window capture, TCI audio probe, RF/relay probes). Load it before deep work on TCI,
SubRX, CAT, console layout or the amplifier/serial hardware.
