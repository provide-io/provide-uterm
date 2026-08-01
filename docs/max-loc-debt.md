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
| `provide-uterm-csharp/tests/.../CoverageTo95Tests.cs` | 1196 | Coverage-driven test batches accreted by wave. Best split along the same batch lines the Makefile already uses. |
| `provide-uterm-csharp/.../Vt/CharsetTables.cs` | 1051 | Generated character tables. A size cap measures nothing actionable here; a reviewer would never read it. Candidate for a permanent generated-file exclusion rather than a split. |
| `scripts/record_uterm_vnc_demo_video.py` | 900 | Pre-existing waiver, carried over from when the check was Python-only. |
| `provide-uterm-go/embed/embed.go` | 831 | Embedded asset wiring. |
| `provide-uterm-csharp/tests/.../CoverageTo97Wave5Tests.cs` | 783 | Six lines over; the cheapest one to clear. |

## Paid down (2026-08-01)

The gate was red here for two separate reasons, and eight files were split to
clear it — but only three of them had baseline entries to delete, so "eight
files split" and "eight entries removed" are not the same count (the commit
message on `83eaabb6` conflates them).

Three ratcheted files had **grown past** their recorded sizes, which is the
never-raise rule firing as designed. Their entries are now deleted:

| File | Entry | Actual | Now |
|---|---:|---:|---|
| `.../Server/UtermServer.cs` | 1423 | 1559 | 358 + `.BridgeRest.cs`/`.BridgeWs.cs` |
| `tests/.../HighCoverageBoostTests.cs` | 1032 | 1035 | two files |
| `tests/.../ServerIntegrationControlPlaneRestTests.cs` | 841 | 924 | two, by route family |

Five more were over the 777 cap with **no entry at all** — they were never
ratcheted, so the gate simply failed on them. Nothing was deleted for these:

| File | Was | Now |
|---|---:|---|
| `tests/.../ResumeLifecycleIntegrationTests.cs` | 3255 | six files by scenario cluster |
| `.../Hub/Lease.cs` | 1556 | 635 + `.Release.cs`/`.Expiry.cs` |
| `provide-uterm-cloudflare/tests/test_owned_input_fencing.py` | 1190 | three modules + `cf_fencing_helpers.py` |
| `.../Hub/Connection.cs` | 1129 | 587 + `.Browser.cs` |
| `tests/.../FanoutExecutionTests.cs` | 812 | two files |

Every resulting file is under 700 lines. Sources became `partial`-class
siblings, tests became partial test classes or sibling modules; whole members
moved verbatim.

## Excluded from the scan

Build output and third-party trees, because a cap on them measures nothing a
reviewer can act on: `bin/`, `obj/` (C# build output), `vendor/` (Go),
`node_modules/`, `.venv/`, `__pycache__/`, `dist/`, `build/`, `mutants/`, and
`.worktrees/`.

Note that `bin`/`obj` are matched as *any* path component, so a source directory
genuinely named `bin/` would be skipped too. That is the right trade here — the
repo's `bin/` directories hold built binaries — but it is worth remembering if
that ever changes.
