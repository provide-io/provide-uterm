#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-testing gate for the Go port's well-covered wire/library packages.

Runs ``gremlins unleash`` (github.com/go-gremlins/gremlins) over a small,
deliberately-scoped perimeter and enforces the same bar as the Python
mutation gate: every mutant must be KILLED, except a documented allowlist of
genuinely-equivalent survivors (``mutation_equivalents.toml``).

Why the perimeter is small: mutation testing recompiles + reruns the package
test binary once per covered mutant, so it is far slower than `go test`. We
scope it to a handful of pure-function packages that are already at ~100%
statement coverage and carry real branch/boundary/arithmetic logic — where a
mutant that survives points at a genuine assertion gap — rather than the whole
~50-package module (which would be slow and dominated by integration packages
whose residual lines need live sockets/PTYs to exercise).

Gate rules (mirroring scripts/run_mutation_gate.py):
  * NOT_COVERED mutant  -> FAIL (coverage gap; the perimeter is 100% covered).
  * TIMED_OUT mutant    -> FAIL. A timeout can mask a real survivor (a
    fast-passing mutant test cut off below `go test` compile time), so we treat
    any timeout as a failure and raise --timeout-coefficient until it clears.
  * LIVED mutant        -> FAIL unless listed in mutation_equivalents.toml.
  * Stale allowlist entry (never matched a survivor) -> WARN (non-fatal).

Stdlib only (tomllib needs Python >= 3.11), so it runs from a bare Go checkout
with just `python3` on PATH.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec
import sys
import tempfile
import tomllib
from pathlib import Path

# Perimeter: pure-function packages already at ~100% statement coverage with
# real boundary/arithmetic logic. Keep this list short so a full run stays a
# couple of minutes. Extend deliberately, one well-covered package at a time.
PERIMETER = (
    "sanitizer",
    "colors",
    "filters",
    "lineeditor",
    "redaction",
    "channels",
    "frames",
    "policy",  # StrictPolicyEngine — cross-language behavior contract
    "defaults",  # TerminalDefaults pure constants/helpers at 100% cover
    "fileio",  # SecureOpenAppend modes + palette/ans pure helpers
)

# Pinned like golangci-lint/govulncheck in the Makefile: invoked via `go run`
# so the version is reproducible without a go.mod tool dependency.
GREMLINS = "github.com/go-gremlins/gremlins/cmd/gremlins@v0.6.0"

# gremlins derives each mutant's test timeout from the (tiny) baseline test
# time; a low coefficient spuriously times out mutants whose recompile exceeds
# the budget — which can hide real survivors. 100 gives every perimeter package
# zero timeouts locally. The gate fails on any timeout, so a too-low value on a
# slow runner surfaces loudly rather than silently.
DEFAULT_COEFFICIENT = 100

MODULE_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_FILE = MODULE_ROOT / "mutation_equivalents.toml"

# gremlins statuses that are never a gate failure. KILLED = caught; NOT_VIABLE =
# the mutation did not compile; SKIPPED = not analysed.
OK_STATUSES = frozenset({"KILLED", "NOT_VIABLE", "SKIPPED"})


def load_allowlist() -> dict[tuple[str, str, int, int, str], str]:
    """Return {(package, file, line, column, mutator): reason} from the TOML."""
    if not ALLOWLIST_FILE.exists():
        return {}
    data = tomllib.loads(ALLOWLIST_FILE.read_text(encoding="utf-8"))
    out: dict[tuple[str, str, int, int, str], str] = {}
    for entry in data.get("equivalent", []):
        key = (
            entry["package"],
            entry["file"],
            int(entry["line"]),
            int(entry["column"]),
            entry["mutator"],
        )
        out[key] = entry["reason"]
    return out


def run_gremlins(package: str, coefficient: int) -> dict:
    """Run `gremlins unleash` on one package and return its parsed JSON report."""
    with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as tmp:
        out_path = Path(tmp.name)
    try:
        cmd = [
            "go",
            "run",
            GREMLINS,
            "unleash",
            "--timeout-coefficient",
            str(coefficient),
            "-o",
            str(out_path),
            f"./{package}/",
        ]
        proc = subprocess.run(  # nosec
            cmd, cwd=MODULE_ROOT, capture_output=True, text=True, check=False
        )
        text = out_path.read_text(encoding="utf-8").strip()
        if not text:
            sys.stdout.write(proc.stdout)
            sys.stderr.write(proc.stderr)
            msg = f"gremlins produced no JSON for {package} (exit {proc.returncode})"
            raise RuntimeError(msg)
        return json.loads(text)
    finally:
        out_path.unlink(missing_ok=True)


def check_package(
    package: str,
    report: dict,
    allowlist: dict[tuple[str, str, int, int, str], str],
    used: set[tuple[str, str, int, int, str]],
) -> tuple[int, int, list[str]]:
    """Return (killed, survivors_ok, failures) for one package report."""
    killed = 0
    excused = 0
    failures: list[str] = []
    for file_entry in report.get("files", []):
        fname = file_entry["file_name"]
        for mut in file_entry.get("mutations", []):
            status = mut["status"]
            if status == "KILLED":
                killed += 1
                continue
            if status in OK_STATUSES:
                continue
            key = (package, fname, int(mut["line"]), int(mut["column"]), mut["type"])
            loc = f"{package}/{fname}:{mut['line']}:{mut['column']} {mut['type']} [{status}]"
            if status == "LIVED" and key in allowlist:
                used.add(key)
                excused += 1
                continue
            failures.append(loc)
    return killed, excused, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout-coefficient",
        type=int,
        default=DEFAULT_COEFFICIENT,
        help=f"gremlins per-mutant timeout coefficient (default {DEFAULT_COEFFICIENT})",
    )
    args = parser.parse_args()

    allowlist = load_allowlist()
    used: set[tuple[str, str, int, int, str]] = set()
    total_killed = 0
    total_excused = 0
    all_failures: list[str] = []

    print(f"Go mutation gate — perimeter: {', '.join(PERIMETER)}")
    print(f"gremlins {GREMLINS.rsplit('@', 1)[1]}, timeout-coefficient {args.timeout_coefficient}\n")

    for package in PERIMETER:
        report = run_gremlins(package, args.timeout_coefficient)
        killed, excused, failures = check_package(package, report, allowlist, used)
        total_killed += killed
        total_excused += excused
        all_failures.extend(failures)
        status = "FAIL" if failures else "ok"
        note = f", {excused} allowlisted-equivalent" if excused else ""
        print(f"  [{status:>4}] {package}: {killed} killed{note}, {len(failures)} unexcused survivor(s)")

    stale = sorted(set(allowlist) - used)
    for key in stale:
        print(f"  [WARN] stale allowlist entry (no longer a survivor): {key[0]}/{key[1]}:{key[2]}:{key[3]} {key[4]}")

    print(f"\nTotals: {total_killed} killed, {total_excused} allowlisted-equivalent, {len(all_failures)} unexcused")
    if all_failures:
        print("\nUnexcused mutants (kill with a test, or document as equivalent in mutation_equivalents.toml):")
        for loc in all_failures:
            print(f"  - {loc}")
        return 1
    print("Go mutation gate passed (every mutant killed or documented-equivalent).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
