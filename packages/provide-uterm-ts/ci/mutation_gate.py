#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Enforce killed==100 on the TypeScript mutation perimeter, minus documented equivalents.

Mirrors the Python gate (scripts/run_mutation_gate.py) and the Go one
(packages/provide-uterm-go/ci/mutation_gate.py): StrykerJS runs the perimeter
declared in stryker.config.mjs, and every surviving mutant must either be killed
by a test or carry a written justification in mutation_equivalents.toml.

Why a wrapper rather than Stryker's own `thresholds.break`: a bare break score
cannot express "these 13 specific mutants are provably equivalent". Setting
break to 100 would fail forever on them; setting it lower would silently accept
any new survivor that fits under the margin. This gate subtracts only the
mutants somebody wrote a reason for, and fails on everything else.

It also reports STALE allowlist entries — ones whose mutant no longer survives,
usually because a source edit moved the line or a later test started killing it.
The allowlist's own header notes that nothing cross-checked it until now, so
expect drift the first time this runs.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_FILE = MODULE_ROOT / "mutation_equivalents.toml"
REPORT_FILE = MODULE_ROOT / "reports" / "mutation" / "mutation.json"

# Stryker statuses that mean "the mutant was not defeated". Timeout and
# RuntimeError are NOT survivors — a mutant that hangs or crashes the runner was
# detected, just not cleanly — but CompileError/Ignored never ran at all.
SURVIVING_STATUSES = frozenset({"Survived", "NoCoverage"})


def _key(file: str, line: int, column: int, mutator: str) -> tuple[str, int, int, str]:
    return (file, int(line), int(column), str(mutator))


def load_allowlist() -> dict[tuple[str, int, int, str], str]:
    if not ALLOWLIST_FILE.exists():
        return {}
    data = tomllib.loads(ALLOWLIST_FILE.read_text())
    out: dict[tuple[str, int, int, str], str] = {}
    for entry in data.get("equivalent", []):
        out[_key(entry["file"], entry["line"], entry["column"], entry["mutator"])] = entry["reason"]
    return out


def run_stryker() -> None:
    subprocess.run(["npx", "stryker", "run"], cwd=MODULE_ROOT, check=False)


def collect_survivors() -> list[dict[str, object]]:
    """Read Stryker's JSON report into a flat survivor list.

    The JSON reporter is used rather than the clear-text one on purpose: the
    clear-text output truncates per-mutant diffs on a run this size, which is
    how a second undocumented survivor once hid behind a documented one in the
    same file (see the note in stryker.config.mjs).
    """
    if not REPORT_FILE.exists():
        print(f"ERROR: no Stryker report at {REPORT_FILE} — did the run fail before reporting?")
        raise SystemExit(2)
    report = json.loads(REPORT_FILE.read_text())
    survivors: list[dict[str, object]] = []
    for path, entry in report.get("files", {}).items():
        for mutant in entry.get("mutants", []):
            if mutant.get("status") in SURVIVING_STATUSES:
                survivors.append(
                    {
                        "file": path,
                        "line": mutant.get("location", {}).get("start", {}).get("line", 0),
                        "column": mutant.get("location", {}).get("start", {}).get("column", 0),
                        "mutator": mutant.get("mutatorName", ""),
                        "status": mutant.get("status"),
                    }
                )
    return survivors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-run",
        action="store_true",
        help="Score an existing reports/mutation/mutation.json instead of re-running Stryker.",
    )
    args = parser.parse_args()

    if not args.skip_run:
        run_stryker()

    allowlist = load_allowlist()
    survivors = collect_survivors()

    keyed = [(s, _key(str(s["file"]), int(s["line"]), int(s["column"]), str(s["mutator"]))) for s in survivors]
    unexcused = [s for s, k in keyed if k not in allowlist]
    excused = [s for s, k in keyed if k in allowlist]
    stale = sorted(set(allowlist) - {k for _, k in keyed})

    # Survivors are counted, not keys. file/line/column/mutator is NOT unique:
    # Stryker can emit several mutants at one position (e.g. two Regex mutants at
    # validators.ts:68:54), so one allowlist entry may excuse more than one
    # survivor. That is the flip side worth knowing — a future mutant landing at
    # the same coordinates with the same mutator would be excused without anyone
    # writing a reason for it. Kept because Stryker's own mutant ids are not
    # stable across runs, which would make an id-keyed allowlist churn constantly.
    print(
        f"TypeScript mutation gate — {len(survivors)} survivor(s): {len(excused)} allowlisted-equivalent, {len(unexcused)} unexcused"
    )

    # Stale entries do not fail the gate: a mutant that stopped surviving is good
    # news. They are printed so the allowlist can be pruned rather than rotting.
    if stale:
        print(f"\nStale allowlist entries ({len(stale)}) — mutant no longer survives, prune them:")
        for file, line, column, mutator in stale:
            print(f"  - {file}:{line}:{column} {mutator}")

    if unexcused:
        print(f"\nUnexcused survivors ({len(unexcused)}) — kill with a test, or document in mutation_equivalents.toml:")
        for s in sorted(unexcused, key=lambda x: (str(x["file"]), int(x["line"]), int(x["column"]))):
            print(f"  - {s['file']}:{s['line']}:{s['column']} {s['mutator']} [{s['status']}]")
        return 1

    print("\nts mutation gate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
