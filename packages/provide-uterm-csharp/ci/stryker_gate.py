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
import fnmatch
import json
import subprocess  # nosec
import sys
import tomllib
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = MODULE_ROOT.parent.parent
SRC = MODULE_ROOT / "src" / "Provide.Uterm"
TEST_PROJECT_DIR = MODULE_ROOT / "tests" / "Provide.Uterm.Tests"
ALLOWLIST_FILE = MODULE_ROOT / "mutation_equivalents.toml"
STRYKER_CONFIG = TEST_PROJECT_DIR / "stryker-config.json"

# Wall-clock budget, mirroring packages/provide-uterm-go/ci/mutation_gate.py.
#
# stryker-config.json sets additional-timeout to 10 minutes. That is now
# insurance rather than the fix. A mutation inside a `static readonly`
# initializer has no per-test coverage for Stryker to derive a leash from (it
# reports `static: true` with an empty coveredBy), so it is measured against the
# WHOLE ~12m suite; at the old 60s allowance a random slice of the 211 such
# mutants in ServerConfig/Load.cs timed out every run (38, then 52, overlapping
# on only 9). Raising the leash fixed the flake and was measured to work -- a
# scoped run reproduced the 2026-08-12 baseline exactly, 333 killed / 2 survived
# / 0 timed out, zero status differences -- but it cost too much wall clock: the
# full perimeter went from 89 to over 115 minutes and stopped fitting the job.
#
# Those 211 are now excluded at the declaration (see the Stryker disable note on
# KnownTopLevelKeys), which removed the timeout class outright: 419 mutants,
# 53 minutes, 0 timeouts. The 10 minute leash stays because nothing else guards
# a future slow mutant, and it costs nothing when none time out.
#
# The budget below is the backstop for a genuinely stuck run. 6000s was picked
# from one data point and killed an honest run at 98 minutes; 6900s then killed
# another at 115. Both were false failures that produced no report at all, since
# Stryker writes one only at the end. 6900s now sits well clear of the measured
# 53 minute run and still below the job's 120 minutes, leaving the gate room to
# print its verdict before GitHub would kill it. A CI runner is slower than the
# laptop these numbers came from; if this starts tripping honest runs, raise the
# job limit rather than guessing here a fourth time.
DEFAULT_BUDGET_S = 6900.0


class GateTimeoutError(RuntimeError):
    """Stryker outran the gate's wall-clock budget."""


# Changing any of these can turn a killed mutant into a survivor (or excuse one)
# without touching a perimeter source file, so a --changed-only run that touches
# them must NOT narrow: it falls back to the full perimeter rather than
# silently reporting "nothing to do". Mirrors the mutation-support-file rule in
# scripts/run_mutation_gate.py.
SUPPORT_PATHS = (
    "packages/provide-uterm-csharp/mutation_equivalents.toml",
    "packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/stryker-config.json",
    "packages/provide-uterm-csharp/ci/stryker_gate.py",
    "packages/provide-uterm-csharp/ci/prepare_stryker_args.sh",
)

# Statuses that are never a gate failure. CompileError is Stryker's own
# Safe-Mode exclusion (the mutation never compiles, so it is never a live
# mutant); Ignored is outside the `mutate` glob or coverage-excluded.
OK_STATUSES = frozenset({"Killed", "CompileError", "Ignored"})

# Allowlist key: CONTENT, not coordinates.
#
# This used to be (file, line, column, mutatorName), which broke twice in one
# afternoon. Line numbers are not a property of a mutant, they are a property of
# everything above it: a 6-line fix in StartDelivery invalidated all sixteen
# entries for Webhooks.Delivery.cs at once, and repairing them meant editing this
# allowlist, which is a SUPPORT_PATH, which forces a full-perimeter run, which
# then blew a CI timeout budgeted for the narrowed one. Coordinates also are not
# unique — Stryker emits two Regex mutants at the same line:column in the TS
# port, so one could be excused by the other's entry.
#
# The key is now what the mutation actually IS:
#   file        the perimeter file, relative to src/Provide.Uterm
#   mutator     Stryker's mutator name
#   code        the source line, stripped — also what makes an entry reviewable
#   original    the exact span Stryker replaced
#   replacement what it replaced the span with
#   occurrence  1-based among mutants in that file identical in all of the above
#
# Everything but `occurrence` is derived from the source text, so unrelated edits
# elsewhere in the file cannot move an entry. Editing the mutated line itself
# DOES invalidate it, which is the intended signal: the equivalence argument was
# about that code, so it has to be re-made.
#
# `occurrence` is computed over EVERY mutant in the file, not just the surviving
# ones, so a mutant that starts or stops being killed cannot renumber its
# neighbours; and the mutant list for a file is a function of its source, not of
# the --mutate glob, so a narrowed run and a full run agree.
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


def file_keys(rel: str, file_entry: dict) -> dict[str, AllowKey]:
    """Map every mutant id in one report file entry to its content key."""
    source = file_entry.get("source", "")
    mutants = file_entry.get("mutants", [])
    # Deterministic order so `occurrence` does not depend on report ordering.
    ordered = sorted(
        mutants,
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
        out[str(mutant["id"])] = (rel, *shape, counts[shape])
    return out


def load_allowlist() -> dict[AllowKey, str]:
    if not ALLOWLIST_FILE.exists():
        return {}
    data = tomllib.loads(ALLOWLIST_FILE.read_text(encoding="utf-8"))
    out: dict[AllowKey, str] = {}
    for entry in data.get("stryker_equivalent", []):
        key: AllowKey = (
            entry["file"].replace("\\", "/"),
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


def perimeter_globs() -> list[str]:
    """The ``mutate`` globs from stryker-config.json — the mutation perimeter."""
    config = json.loads(STRYKER_CONFIG.read_text(encoding="utf-8"))
    return list(config["stryker-config"]["mutate"])


def _git_changed_paths(base_ref: str) -> list[str]:
    """Repo-relative paths changed between ``base_ref`` and the working tree."""
    proc = subprocess.run(  # nosec
        ["git", "diff", "--name-only", base_ref],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(f"FAIL: git diff against {base_ref!r} failed: {proc.stderr.strip()}", file=sys.stderr)
        raise SystemExit(2)
    return [line for line in proc.stdout.splitlines() if line]


def changed_perimeter_globs(base_ref: str) -> list[str] | None:
    """Narrowed ``--mutate`` globs for the changed perimeter files.

    Returns ``None`` when the run must NOT be narrowed (a support file changed,
    so the whole perimeter has to be re-proven), and ``[]`` when nothing on the
    perimeter changed at all (the caller skips the run entirely).
    """
    changed = _git_changed_paths(base_ref)
    if any(path in SUPPORT_PATHS for path in changed):
        return None

    src_prefix = f"{SRC.relative_to(REPO_ROOT).as_posix()}/"
    globs = perimeter_globs()
    selected: list[str] = []
    for path in changed:
        if not path.startswith(src_prefix):
            continue
        rel = path[len(src_prefix) :]
        for glob in globs:
            # Perimeter globs are written `**/Server/Foo.cs`; match them against
            # the path relative to the mutated project root.
            if fnmatch.fnmatch(rel, glob.removeprefix("**/")) or fnmatch.fnmatch(rel, glob):
                selected.append(glob)
                break
    return sorted(set(selected))


def run_stryker(mutate: list[str] | None = None, budget_s: float = DEFAULT_BUDGET_S) -> Path:
    """Run ``dotnet stryker`` and return the path to its JSON report."""
    cmd = ["dotnet", "stryker"]
    for glob in mutate or []:
        cmd += ["--mutate", glob]
    scope = ", ".join(mutate) if mutate else "the full perimeter"
    try:
        proc = subprocess.run(cmd, cwd=TEST_PROJECT_DIR, check=False, timeout=budget_s)  # nosec
    except subprocess.TimeoutExpired as expired:
        # A slow runner and a stuck mutant look identical from here, so name
        # both readings rather than asserting one of them.
        raise GateTimeoutError(
            f"dotnet stryker exceeded the gate's {budget_s:.0f}s budget while mutating {scope}. "
            "Either the runner made no progress, or additional-timeout in stryker-config.json is "
            "now long enough that a stuck mutant can spend the whole budget by itself."
        ) from expired
    if proc.returncode not in (0, 1):
        # 0 = above threshold-break, 1 = below it (Stryker's own thresholds are
        # informational here; we compute our own kill bar below). Anything
        # else means Stryker itself failed to run (build error, bad config).
        print(f"FAIL: dotnet stryker exited {proc.returncode} (tool failure, not a mutation result)", file=sys.stderr)
        raise SystemExit(2)

    output_root = TEST_PROJECT_DIR / "StrykerOutput"
    if not output_root.is_dir():
        # Stryker exits 1 both for "below threshold" and for "the tool is not
        # installed", so the return code above cannot tell them apart. No
        # output directory at all means it never ran — most often the local
        # tool was not restored.
        print(
            f"FAIL: {output_root} does not exist — dotnet stryker produced no output. "
            "Run `dotnet tool restore` first (see .config/dotnet-tools.json).",
            file=sys.stderr,
        )
        raise SystemExit(2)
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
    parser.add_argument(
        "--changed-only",
        action="store_true",
        help="Mutate only the perimeter files changed since --base-ref; skip entirely if none did.",
    )
    parser.add_argument(
        "--base-ref",
        default="HEAD",
        help="Git base ref used for --changed-only (default: HEAD, i.e. the working tree).",
    )
    args = parser.parse_args()

    allowlist = load_allowlist()
    used: set[AllowKey] = set()

    narrowed: list[str] | None = None
    if args.changed_only and args.report is None:
        selected = changed_perimeter_globs(args.base_ref)
        if selected is None:
            print("Mutation support file changed — running the full perimeter rather than narrowing.")
        elif not selected:
            # The whole point of --changed-only: a full Stryker run pays a fixed
            # ~10-20 minute whole-project instrumentation cost even when the
            # perimeter glob is narrow, and most pushes touch none of it.
            print(f"SKIP: no perimeter file changed since {args.base_ref} — nothing to mutate.")
            return 0
        else:
            narrowed = selected
            print(f"Narrowed to {len(narrowed)} changed perimeter file(s): {', '.join(narrowed)}")

    try:
        report_path = args.report or run_stryker(narrowed)
    except GateTimeoutError as timed_out:
        # A verdict, not a traceback: this path exists because the job being
        # killed from outside printed nothing about where it was.
        print(f"FAIL: {timed_out}", file=sys.stderr)
        return 2
    report = json.loads(report_path.read_text(encoding="utf-8"))

    killed = 0
    excused = 0
    failures: list[str] = []
    seen_files: set[str] = set()

    for abs_path, file_entry in report.get("files", {}).items():
        try:
            rel = str(Path(abs_path).resolve().relative_to(SRC)).replace("\\", "/")
        except ValueError:
            # Not under src/Provide.Uterm — Stryker instruments the whole
            # project but our perimeter (and therefore our allowlist) only
            # ever names files inside it.
            continue

        keys_by_id = file_keys(rel, file_entry)

        for mutant in file_entry.get("mutants", []):
            status = mutant["status"]
            if status != "Ignored":
                seen_files.add(rel)
            if status in OK_STATUSES:
                if status == "Killed":
                    killed += 1
                continue

            loc = mutant["location"]["start"]
            key = keys_by_id[str(mutant["id"])]
            # Coordinates are still the most useful thing to PRINT — they are just
            # no longer what the entry is matched on.
            where = f"{rel}:{loc['line']}:{loc['column']} {mutant['mutatorName']} [{status}]"
            if key in allowlist:
                used.add(key)
                excused += 1
                continue
            failures.append(where)

    # On a narrowed run, allowlist entries for files outside the narrowed set were
    # never mutated, so their absence says nothing about staleness.
    checked_files = seen_files if narrowed else None
    stale = sorted(key for key in set(allowlist) - used if checked_files is None or key[0] in checked_files)
    for key in stale:
        print(f"[WARN] stale allowlist entry (no longer a survivor): {describe(key)}")

    print(f"\nStryker mutation gate — report: {report_path}")
    print(f"Totals: {killed} killed, {excused} allowlisted-equivalent, {len(failures)} unexcused")

    if failures:
        print("\nUnexcused mutants (kill with a test, or document as equivalent in mutation_equivalents.toml):")
        for loc in failures:
            print(f"  - {loc}")
        return 1

    if narrowed and not seen_files:
        # An explicitly-narrowed run that produced no live mutants at all means
        # the narrowing did not reach the code it named — a broken --mutate
        # glob reads exactly like a clean pass otherwise.
        print(
            f"FAIL: narrowed to {', '.join(narrowed)} but Stryker mutated nothing there.",
            file=sys.stderr,
        )
        return 2

    print("Stryker mutation gate passed (every mutant killed or documented-equivalent).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
