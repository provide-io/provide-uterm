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


def run_stryker(mutate: list[str] | None = None) -> Path:
    """Run ``dotnet stryker`` and return the path to its JSON report."""
    cmd = ["dotnet", "stryker"]
    for glob in mutate or []:
        cmd += ["--mutate", glob]
    proc = subprocess.run(cmd, cwd=TEST_PROJECT_DIR, check=False)  # nosec
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

    report_path = args.report or run_stryker(narrowed)
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

        for mutant in file_entry.get("mutants", []):
            status = mutant["status"]
            if status != "Ignored":
                seen_files.add(rel)
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

    # On a narrowed run, allowlist entries for files outside the narrowed set were
    # never mutated, so their absence says nothing about staleness.
    checked_files = seen_files if narrowed else None
    stale = sorted(key for key in set(allowlist) - used if checked_files is None or key[0] in checked_files)
    for key in stale:
        print(f"[WARN] stale allowlist entry (no longer a survivor): {key[0]}:{key[1]}:{key[2]} {key[3]}")

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
