#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Real mutation-testing gate for the C# port, using Stryker.NET.

Runs ``dotnet stryker`` (installed as a local tool — see
``.config/dotnet-tools.json``) against ``tests/Provide.Uterm.Tests/stryker-config.json``
and enforces the same bar Python's mutmut gate and Go's gremlins gate do: every
mutant on the perimeter must be KILLED, except a documented allowlist of
genuinely-equivalent survivors (``mutation_equivalents.toml``).

This is the *real* mutation-testing gate (comprehensive Roslyn-level mutators:
boundary, arithmetic, conditional, string, statement, …), distinct from the
older ``ci/mutation_gate.py``, which is a small hand-rolled ``&&``/``||``
flipper predating this tool and still covers its own (non-overlapping)
perimeter. ``make mutation-gate`` runs both.

Why a whole-project instrumentation cost is unavoidable: Stryker compiles one
instrumented copy of the *entire* mutated project (here, all of
``Provide.Uterm``) regardless of how narrow the ``mutate`` glob is — C#
compiles per-assembly, not per-file — so every run pays a fixed ~10 minute
analysis/build cost before any perimeter-specific testing starts. The
``mutate`` glob in ``stryker-config.json`` is what actually narrows which of
the resulting mutants are *tested* (everything else is reported ``Ignored``).

Perimeter (``stryker-config.json`` ``mutate``): the CIDR/SSRF egress classifier,
the connector and webhook egress guards, the webhook registry + delivery loop
(retry ladder, HMAC signing, auto-unregister), and the TOML config binder —
security-critical, state-machine, and wire-format-adjacent surfaces, matching
the same philosophy as Python's ``MUTATION_PATTERNS.md`` and Go's
``mutation_equivalents.toml``.

Gate rules (mirroring ``scripts/run_mutation_gate.py`` and the Go gate):
  * ``Survived`` / ``NoCoverage`` / ``Timeout`` mutant -> FAIL unless listed in
    ``mutation_equivalents.toml``. ``NoCoverage`` is a coverage gap, never
    excusable by "wasn't reached" alone — but see the allowlist notes for the
    handful of genuinely-unreachable-through-the-public-API exceptions, which
    are equivalence claims about *observability*, not about coverage.
  * ``Killed`` / ``CompileError`` / ``Ignored`` -> fine. ``CompileError``
    mutants are Stryker's own "Safe Mode" compile-safety exclusions (never a
    real mutant); ``Ignored`` mutants are outside the ``mutate`` glob or
    coverage-excluded.
  * A stale allowlist entry (its mutant was later killed, or moved by a source
    edit) is reported as a WARNING, never a failure.

Stdlib only (Python >= 3.11 for tomllib). Requires ``dotnet`` on PATH and the
local tool restored (``dotnet tool restore``).
"""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec
import sys
import tomllib
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parent.parent
SRC = MODULE_ROOT / "src" / "Provide.Uterm"
TEST_PROJECT_DIR = MODULE_ROOT / "tests" / "Provide.Uterm.Tests"
ALLOWLIST_FILE = MODULE_ROOT / "mutation_equivalents.toml"

# Statuses that are never a gate failure. CompileError is Stryker's own
# Safe-Mode exclusion (the mutation never compiles, so it is never a live
# mutant); Ignored is outside the `mutate` glob or coverage-excluded.
OK_STATUSES = frozenset({"Killed", "CompileError", "Ignored"})

# Stryker key: (file relative to src/Provide.Uterm, line, column, mutatorName).
AllowKey = tuple[str, int, int, str]


def load_allowlist() -> dict[AllowKey, str]:
    if not ALLOWLIST_FILE.exists():
        return {}
    data = tomllib.loads(ALLOWLIST_FILE.read_text(encoding="utf-8"))
    out: dict[AllowKey, str] = {}
    for entry in data.get("stryker_equivalent", []):
        key = (
            entry["file"].replace("\\", "/"),
            int(entry["line"]),
            int(entry["column"]),
            entry["mutator"],
        )
        out[key] = entry["reason"]
    return out


def run_stryker() -> Path:
    """Run ``dotnet stryker`` and return the path to its JSON report."""
    cmd = ["dotnet", "stryker"]
    proc = subprocess.run(cmd, cwd=TEST_PROJECT_DIR, check=False)  # nosec
    if proc.returncode not in (0, 1):
        # 0 = above threshold-break, 1 = below it (Stryker's own thresholds are
        # informational here; we compute our own kill bar below). Anything
        # else means Stryker itself failed to run (build error, bad config).
        print(f"FAIL: dotnet stryker exited {proc.returncode} (tool failure, not a mutation result)", file=sys.stderr)
        raise SystemExit(2)

    output_root = TEST_PROJECT_DIR / "StrykerOutput"
    runs = sorted((p for p in output_root.iterdir() if p.is_dir()), key=lambda p: p.name)
    if not runs:
        print("FAIL: no StrykerOutput run directory found", file=sys.stderr)
        raise SystemExit(2)
    report = runs[-1] / "reports" / "mutation-report.json"
    if not report.is_file():
        print(f"FAIL: no mutation-report.json under {runs[-1]}", file=sys.stderr)
        raise SystemExit(2)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Use an existing mutation-report.json instead of running Stryker again.",
    )
    args = parser.parse_args()

    allowlist = load_allowlist()
    used: set[AllowKey] = set()

    report_path = args.report or run_stryker()
    report = json.loads(report_path.read_text(encoding="utf-8"))

    killed = 0
    excused = 0
    failures: list[str] = []

    for abs_path, file_entry in report.get("files", {}).items():
        try:
            rel = str(Path(abs_path).resolve().relative_to(SRC)).replace("\\", "/")
        except ValueError:
            # Not under src/Provide.Uterm — Stryker instruments the whole
            # project but our perimeter (and therefore our allowlist) only
            # ever names files inside it.
            continue

        for mutant in file_entry.get("mutants", []):
            status = mutant["status"]
            if status in OK_STATUSES:
                if status == "Killed":
                    killed += 1
                continue

            loc = mutant["location"]["start"]
            key: AllowKey = (rel, int(loc["line"]), int(loc["column"]), mutant["mutatorName"])
            where = f"{rel}:{loc['line']}:{loc['column']} {mutant['mutatorName']} [{status}]"
            if key in allowlist:
                used.add(key)
                excused += 1
                continue
            failures.append(where)

    stale = sorted(set(allowlist) - used)
    for key in stale:
        print(f"[WARN] stale allowlist entry (no longer a survivor): {key[0]}:{key[1]}:{key[2]} {key[3]}")

    print(f"\nStryker mutation gate — report: {report_path}")
    print(f"Totals: {killed} killed, {excused} allowlisted-equivalent, {len(failures)} unexcused")

    if failures:
        print("\nUnexcused mutants (kill with a test, or document as equivalent in mutation_equivalents.toml):")
        for loc in failures:
            print(f"  - {loc}")
        return 1

    print("Stryker mutation gate passed (every mutant killed or documented-equivalent).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
