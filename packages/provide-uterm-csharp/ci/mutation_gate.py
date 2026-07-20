#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation gate for the C# port — pure-library perimeter.

Mirrors the Go ``ci/mutation_gate.py`` bar: every applied mutant on the
perimeter must be KILLED by the filtered test suite, unless documented in
``mutation_equivalents.toml``.

Perimeter (deliberately small pure-logic surfaces with branchy ``&&``/``||``):
  * Policy/StrictPolicyEngine.cs
  * DeckMux/PresenceService.cs
  * Colors/Sgr.cs
  * Filters/Filters.cs (when it has boolean ops)

Stdlib only (Python >= 3.11 for tomllib). Requires ``dotnet`` on PATH.
"""

from __future__ import annotations

import argparse
import re
import subprocess  # nosec
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parent.parent
SRC = MODULE_ROOT / "src" / "Provide.Uterm"
ALLOWLIST_FILE = MODULE_ROOT / "mutation_equivalents.toml"
TEST_PROJECT = MODULE_ROOT / "tests" / "Provide.Uterm.Tests" / "Provide.Uterm.Tests.csproj"

PERIMETER = (
    SRC / "Policy" / "StrictPolicyEngine.cs",
    SRC / "DeckMux" / "PresenceService.cs",
    SRC / "Colors" / "Sgr.cs",
    SRC / "Filters" / "Filters.cs",
    SRC / "Sanitizer" / "Sanitizer.cs",
    SRC / "Redaction" / "Redaction.cs",
    SRC / "Auth" / "Auth.cs",  # authorized_keys + option parse boolean ops
    SRC / "Channels" / "Channels.cs",  # hello/grant boolean negotiation arms
)

# Operator flips that exercise real branch logic. Boolean literal flips in
# C# produce too many equivalent survivors (default field inits, ternary
# arms already covered by neighbors); keep the perimeter to boolean ops.
MUTATORS: list[tuple[str, str, str]] = [
    ("and_or", r"&&", "||"),
    ("or_and", r"\|\|", "&&"),
]


@dataclass(frozen=True)
class Mutant:
    path: Path
    line: int
    col: int
    mutator: str
    original: str
    replacement: str
    source: str


def load_allowlist() -> dict[tuple[str, str, int, str], str]:
    if not ALLOWLIST_FILE.exists():
        return {}
    data = tomllib.loads(ALLOWLIST_FILE.read_text(encoding="utf-8"))
    out: dict[tuple[str, str, int, str], str] = {}
    for entry in data.get("equivalent", []):
        key = (
            entry["file"],
            entry.get("package", "Provide.Uterm"),
            int(entry["line"]),
            entry["mutator"],
        )
        out[key] = entry["reason"]
    return out


def collect_mutants(path: Path) -> list[Mutant]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    mutants: list[Mutant] = []
    # Skip SPDX / using / namespace / comment-only lines for speed.
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "using ", "namespace ", "[", "#")):
            continue
        for name, pattern, repl in MUTATORS:
            for m in re.finditer(pattern, line):
                # Avoid flipping true/false inside strings roughly.
                before = line[: m.start()]
                if before.count('"') % 2 == 1:
                    continue
                mutants.append(
                    Mutant(
                        path=path,
                        line=i,
                        col=m.start() + 1,
                        mutator=name,
                        original=m.group(0),
                        replacement=repl,
                        source=text,
                    )
                )
    return mutants


def apply_mutant(m: Mutant) -> str:
    lines = m.source.splitlines(keepends=True)
    idx = m.line - 1
    line = lines[idx]
    # Replace only the occurrence at m.col
    col = m.col - 1
    end = col + len(m.original)
    if line[col:end] != m.original:
        # Fall back to first occurrence on the line.
        pos = line.find(m.original)
        if pos < 0:
            raise RuntimeError(f"cannot apply mutant {m}")
        col, end = pos, pos + len(m.original)
    lines[idx] = line[:col] + m.replacement + line[end:]
    return "".join(lines)


def run_tests() -> bool:
    # Focused tests for perimeter (Policy + DeckMux + Colors/Sgr + Filters + Sanitizer + Redaction).
    filt = (
        "FullyQualifiedName~StrictPolicy|FullyQualifiedName~Policy|"
        "FullyQualifiedName~DeckMux|FullyQualifiedName~PresenceService|"
        "FullyQualifiedName~Sgr|FullyQualifiedName~AnsiColors|"
        "FullyQualifiedName~Filters|FullyQualifiedName~Sanitizer|"
        "FullyQualifiedName~Redaction|FullyQualifiedName~Auth|FullyQualifiedName~Channels"
    )
    cmd = [
        "dotnet",
        "test",
        str(TEST_PROJECT),
        "-c",
        "Release",
        "--nologo",
        "-v",
        "q",
        "--no-build",
        "--filter",
        filt,
        "--",
        "xUnit.ParallelizeAssembly=false",
        "xUnit.MaxParallelThreads=1",
    ]
    proc = subprocess.run(cmd, cwd=MODULE_ROOT, capture_output=True, text=True)  # nosec
    # Mutant is killed if tests fail (non-zero).
    return proc.returncode != 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-mutants", type=int, default=0, help="Cap mutants (0=all)")
    args = parser.parse_args()

    allow = load_allowlist()
    all_mutants: list[Mutant] = []
    for path in PERIMETER:
        if not path.is_file():
            print(f"FAIL: perimeter missing {path}", file=sys.stderr)
            return 2
        all_mutants.extend(collect_mutants(path))

    if args.max_mutants > 0:
        all_mutants = all_mutants[: args.max_mutants]

    # Build once so --no-build per mutant is valid.
    build = subprocess.run(  # nosec
        ["dotnet", "build", str(TEST_PROJECT), "-c", "Release", "--nologo", "-v", "q"],
        cwd=MODULE_ROOT,
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        print(build.stdout[-2000:], file=sys.stderr)
        print(build.stderr[-2000:], file=sys.stderr)
        print("FAIL: dotnet build for mutation gate", file=sys.stderr)
        return 2

    print(f"C# mutation gate: {len(all_mutants)} mutants across {len(PERIMETER)} files")
    lived: list[Mutant] = []
    killed = 0
    allowed = 0
    matched_allow: set[tuple[str, str, int, str]] = set()

    for i, m in enumerate(all_mutants, start=1):
        rel = str(m.path.relative_to(SRC))
        key = (rel.replace("\\", "/"), "Provide.Uterm", m.line, m.mutator)
        mutated = apply_mutant(m)
        backup = m.path.read_text(encoding="utf-8")
        try:
            m.path.write_text(mutated, encoding="utf-8")
            # Rebuild the mutated library so --no-build tests see the mutant.
            rebuild = subprocess.run(  # nosec
                [
                    "dotnet",
                    "build",
                    str(MODULE_ROOT / "src" / "Provide.Uterm" / "Provide.Uterm.csproj"),
                    "-c",
                    "Release",
                    "--nologo",
                    "-v",
                    "q",
                ],
                cwd=MODULE_ROOT,
                capture_output=True,
                text=True,
            )
            if rebuild.returncode != 0:
                # Mutant did not compile → treat as killed (not viable).
                dead = True
            else:
                # Rebuild test host so it picks up the mutated library.
                tbuild = subprocess.run(  # nosec
                    [
                        "dotnet",
                        "build",
                        str(TEST_PROJECT),
                        "-c",
                        "Release",
                        "--nologo",
                        "-v",
                        "q",
                    ],
                    cwd=MODULE_ROOT,
                    capture_output=True,
                    text=True,
                )
                dead = tbuild.returncode != 0 or run_tests()
        finally:
            m.path.write_text(backup, encoding="utf-8")
            # Restore a clean build for the next mutant.
            subprocess.run(  # nosec
                [
                    "dotnet",
                    "build",
                    str(TEST_PROJECT),
                    "-c",
                    "Release",
                    "--nologo",
                    "-v",
                    "q",
                ],
                cwd=MODULE_ROOT,
                capture_output=True,
                text=True,
            )

        if dead:
            killed += 1
            status = "KILLED"
        elif key in allow:
            allowed += 1
            matched_allow.add(key)
            status = "EQUIV"
        else:
            lived.append(m)
            status = "LIVED"
        print(f"  [{i}/{len(all_mutants)}] {status} {rel}:{m.line} {m.mutator} {m.original!r}->{m.replacement!r}")

    for key, reason in allow.items():
        if key not in matched_allow:
            print(f"WARN: stale allowlist entry {key}: {reason}")

    print(f"Summary: killed={killed} allowed={allowed} lived={len(lived)} total={len(all_mutants)}")
    if lived:
        print("LIVED mutants (not on allowlist):", file=sys.stderr)
        for m in lived:
            print(f"  {m.path.name}:{m.line} {m.mutator}", file=sys.stderr)
        return 1
    if killed + allowed == 0:
        print("FAIL: zero mutants applied", file=sys.stderr)
        return 1
    print("mutation-gate PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
