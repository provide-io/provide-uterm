# Max-LOC debt

The 777-line cap (`scripts/check_max_loc.py`, run by `ci/quality_checks.sh`)
applies to `.py`, `.cs` and `.go` alike. It previously scanned Python only,
which is why the largest files in the repo are C#: nothing was measuring them.

## How the ratchet works

`.ci/max-loc-baseline.json` records each already-oversized file at its size on
the day its language came under the cap. An entry permits **that size and no
more**, so a listed file can shrink but never grow, and any *new* oversized file
fails outright. Adding an entry is a deferral, not an exemption.

To pay one down: split the file, then lower or delete its baseline entry in the
same change. Never raise an entry — if a file needs to grow past its number, it
needs splitting instead.

## Currently deferred

| File | Lines | Why it is still here |
|---|---:|---|
| `provide-uterm-csharp/.../Server/UtermServer.cs` | 1562 | The server type is already `partial` and split across `UtermServer.Gui.cs`, `.GraphicalTargets.cs`, `.HostRest.cs`, `.HumanVnc.cs` and others. This file is the remaining core: construction, route mapping, `ServerFactory`. Splitting it is mechanical but touches route registration, so it wants its own change. |
| `provide-uterm-csharp/tests/.../CoverageTo95Tests.cs` | 1196 | Coverage-driven test batches accreted by wave. Best split along the same batch lines the Makefile already uses. |
| `provide-uterm-csharp/.../Vt/CharsetTables.cs` | 1051 | Generated character tables. A size cap measures nothing actionable here; a reviewer would never read it. Candidate for a permanent generated-file exclusion rather than a split. |
| `provide-uterm-csharp/tests/.../HighCoverageBoostTests.cs` | 1032 | Same accretion as `CoverageTo95Tests.cs`. |
| `scripts/record_uterm_vnc_demo_video.py` | 900 | Pre-existing waiver, carried over from when the check was Python-only. |
| `provide-uterm-csharp/tests/.../ServerIntegrationControlPlaneRestTests.cs` | 836 | REST integration cases; splits cleanly by route family. |
| `provide-uterm-go/embed/embed.go` | 831 | Embedded asset wiring. |
| `provide-uterm-csharp/tests/.../CoverageTo97Wave5Tests.cs` | 783 | Six lines over; the cheapest one to clear. |

## Excluded from the scan

Build output and third-party trees, because a cap on them measures nothing a
reviewer can act on: `bin/`, `obj/` (C# build output), `vendor/` (Go),
`node_modules/`, `.venv/`, `__pycache__/`, `dist/`, `build/`, `mutants/`, and
`.worktrees/`.

Note that `bin`/`obj` are matched as *any* path component, so a source directory
genuinely named `bin/` would be skipped too. That is the right trade here — the
repo's `bin/` directories hold built binaries — but it is worth remembering if
that ever changes.
