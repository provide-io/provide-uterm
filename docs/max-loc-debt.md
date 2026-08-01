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

Split into `partial`-class siblings (sources) or partial test classes / sibling
modules (tests), every resulting file under 700 lines, and their baseline
entries deleted: `UtermServer.cs` (1559 → 358 + `.BridgeRest.cs`/`.BridgeWs.cs`),
`Hub/Lease.cs` (1556 → 635 + `.Release.cs`/`.Expiry.cs`), `Hub/Connection.cs`
(1129 → 587 + `.Browser.cs`), `ResumeLifecycleIntegrationTests.cs` (3255 → six
files), `HighCoverageBoostTests.cs` (1035 → two), 
`ServerIntegrationControlPlaneRestTests.cs` (924 → two, by route family),
`FanoutExecutionTests.cs` (812 → two), and the Cloudflare
`test_owned_input_fencing.py` (1190 → three modules + `cf_fencing_helpers.py`).

## Excluded from the scan

Build output and third-party trees, because a cap on them measures nothing a
reviewer can act on: `bin/`, `obj/` (C# build output), `vendor/` (Go),
`node_modules/`, `.venv/`, `__pycache__/`, `dist/`, `build/`, `mutants/`, and
`.worktrees/`.

Note that `bin`/`obj` are matched as *any* path component, so a source directory
genuinely named `bin/` would be skipped too. That is the right trade here — the
repo's `bin/` directories hold built binaries — but it is worth remembering if
that ever changes.
