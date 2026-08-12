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


# Allowlist key: CONTENT, not coordinates. Identical to the C# gate's
# (packages/provide-uterm-csharp/ci/stryker_gate.py) — same reporter, same
# problem, same fix, and the two are meant to stay recognisably the same.
#
# file/line/column/mutator failed in both directions at once. It was not stable:
# inserting lines above a mutant moved every entry below it, and since this file
# is a mutation support file, repairing the numbers itself triggers a full
# perimeter run. It was also not unique: Stryker emits two Regex mutants at
# validators.ts:68:54, so one entry silently excused both, and a future third
# mutant at those coordinates would have been excused without a reason.
#
#   file        perimeter file, as the report names it
#   mutator     Stryker's mutator name
#   code        the source line, stripped — also what makes an entry reviewable
#   original    the exact span Stryker replaced
#   replacement what it replaced the span with
#   occurrence  1-based among mutants in that file identical in all of the above
#
# The two Regex mutants differ in `replacement`, so they now key apart. Where
# they genuinely do not differ, `occurrence` separates them and each needs its
# own reason.
AllowKey = tuple[str, str, str, str, str, int]


def mutant_span(source: str, location: dict) -> str:
    """The exact source text Stryker replaced (1-based, end-exclusive)."""
    lines = source.splitlines(keepends=True)
    start, end = location["start"], location["end"]
    if start["line"] == end["line"]:
        return lines[start["line"] - 1][start["column"] - 1 : end["column"] - 1]
    body = [lines[start["line"] - 1][start["column"] - 1 :]]
    body += lines[start["line"] : end["line"] - 1]
    body.append(lines[end["line"] - 1][: end["column"] - 1])
    return "".join(body)


def source_line(source: str, location: dict) -> str:
    """The stripped source line the mutant starts on."""
    lines = source.splitlines()
    index = location["start"]["line"] - 1
    return lines[index].strip() if 0 <= index < len(lines) else ""


def file_keys(path: str, file_entry: dict) -> dict[str, AllowKey]:
    """Map every mutant id in one report file entry to its content key.

    `occurrence` counts over EVERY mutant in the file, not just survivors, so a
    mutant that starts or stops being killed cannot renumber its neighbours.
    """
    source = file_entry.get("source", "")
    ordered = sorted(
        file_entry.get("mutants", []),
        key=lambda m: (
            m["location"]["start"]["line"],
            m["location"]["start"]["column"],
            m["mutatorName"],
            m.get("replacement", ""),
        ),
    )
    counts: dict[tuple[str, str, str, str], int] = {}
    out: dict[str, AllowKey] = {}
    for mutant in ordered:
        location = mutant["location"]
        shape = (
            mutant["mutatorName"],
            source_line(source, location),
            mutant_span(source, location),
            mutant.get("replacement", ""),
        )
        counts[shape] = counts.get(shape, 0) + 1
        out[str(mutant["id"])] = (path, *shape, counts[shape])
    return out


def load_allowlist() -> dict[AllowKey, str]:
    if not ALLOWLIST_FILE.exists():
        return {}
    data = tomllib.loads(ALLOWLIST_FILE.read_text())
    out: dict[AllowKey, str] = {}
    for entry in data.get("equivalent", []):
        key: AllowKey = (
            entry["file"],
            entry["mutator"],
            entry["code"],
            entry["original"],
            entry["replacement"],
            int(entry.get("occurrence", 1)),
        )
        out[key] = entry["reason"]
    return out


def describe(key: AllowKey) -> str:
    """One-line rendering of a key, for stale-entry warnings."""
    file, mutator, code, original, replacement, occurrence = key
    nth = f" #{occurrence}" if occurrence > 1 else ""
    return f"{file} {mutator}{nth}: {original!r} -> {replacement!r} in {code!r}"


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
        keys_by_id = file_keys(path, entry)
        for mutant in entry.get("mutants", []):
            if mutant.get("status") in SURVIVING_STATUSES:
                survivors.append(
                    {
                        "file": path,
                        # Coordinates are still the most useful thing to PRINT;
                        # they are just no longer what an entry is matched on.
                        "line": mutant.get("location", {}).get("start", {}).get("line", 0),
                        "column": mutant.get("location", {}).get("start", {}).get("column", 0),
                        "mutator": mutant.get("mutatorName", ""),
                        "status": mutant.get("status"),
                        "key": keys_by_id[str(mutant["id"])],
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

    keyed = [(s, s["key"]) for s in survivors]
    unexcused = [s for s, k in keyed if k not in allowlist]
    excused = [s for s, k in keyed if k in allowlist]
    stale = sorted(set(allowlist) - {k for _, k in keyed})

    # Survivors are counted, not keys — but unlike the old coordinate key, one
    # entry can no longer quietly cover several survivors: `occurrence` makes the
    # key unique per mutant, so each excused survivor has a reason written for it.
    print(
        f"TypeScript mutation gate — {len(survivors)} survivor(s): {len(excused)} allowlisted-equivalent, {len(unexcused)} unexcused"
    )

    # Stale entries do not fail the gate: a mutant that stopped surviving is good
    # news. They are printed so the allowlist can be pruned rather than rotting.
    if stale:
        print(f"\nStale allowlist entries ({len(stale)}) — mutant no longer survives, prune them:")
        for key in stale:
            print(f"  - {describe(key)}")

    if unexcused:
        print(f"\nUnexcused survivors ({len(unexcused)}) — kill with a test, or document in mutation_equivalents.toml:")
        for s in sorted(unexcused, key=lambda x: (str(x["file"]), int(x["line"]), int(x["column"]))):
            print(f"  - {s['file']}:{s['line']}:{s['column']} {s['mutator']} [{s['status']}]")
        return 1

    print("\nts mutation gate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
