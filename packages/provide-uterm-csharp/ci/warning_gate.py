#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Ratchet the C# build's compiler/analyzer warnings against a committed baseline.

The port sat at eight warnings for long enough that they read as scenery. Two of
them were not scenery at all:

  * CS0649 on ``InMemoryGraphicalTargetRegistry._closed`` was the only signal
    that the port had the closed-state guard but never the ``Close()`` that sets
    it, so ``GraphicalTargetErrorCode.Closed`` was unreachable while the
    reference and the TypeScript port both expose ``close()``.
  * CS0414 on ``MemoryEngine._open`` advertised a closed-state guard that the
    reference deliberately does not have.

Neither could be found by a test, because both were about code that could not
run. A warning count is therefore a real signal here and worth a gate.

Why a baseline file rather than ``TreatWarningsAsErrors``: a new SDK can
introduce a whole warning class overnight, and turning that into "the build is
broken" gives whoever hits it no way to land an unrelated fix. An entry here is
a deferral with a written reason, and the gate still fails on anything not
listed. The baseline is currently EMPTY -- keep it that way if you can.

Usage:
    dotnet build -c Release -v normal | python3 ci/warning_gate.py [baseline.json]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# MSBuild normal verbosity: "<path>(line,col): warning CS0649: <text> [<proj>]".
# The project suffix is dropped: the same warning is reported once per project
# that references the assembly, and the location is what identifies it.
WARNING_RE = re.compile(
    r"(?P<file>[^\s>]+\.cs)\((?P<line>\d+),(?P<col>\d+)\): warning (?P<code>[A-Z]+\d+): (?P<text>.*?)(?: \[[^\]]*\])?$"
)

DEFAULT_BASELINE = Path(__file__).resolve().parent / "warning-baseline.json"


def parse(stream: object) -> set[tuple[str, str]]:
    """Return the distinct ``(code, file)`` warnings in an MSBuild log.

    Keyed on code+file rather than code+line+column so that inserting a line
    above a deferred warning does not invalidate its baseline entry -- the same
    trap the port's mutation allowlists hit before they moved to content keys.
    """
    found: set[tuple[str, str]] = set()
    for raw in stream:  # type: ignore[attr-defined]
        match = WARNING_RE.search(raw.rstrip("\n"))
        if match is None:
            continue
        path = match.group("file").replace("\\", "/")
        # Absolute paths differ between a laptop and a runner; anchor on the
        # package-relative part so a baseline entry is portable.
        marker = "provide-uterm-csharp/"
        if marker in path:
            path = path.split(marker, 1)[1]
        found.add((match.group("code"), path))
    return found


def load_baseline(path: Path) -> set[tuple[str, str]]:
    if not path.is_file():
        return set()
    document = json.loads(path.read_text(encoding="utf-8"))
    return {(entry["code"], entry["file"]) for entry in document.get("allowed", [])}


def main(argv: list[str]) -> int:
    baseline_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_BASELINE
    allowed = load_baseline(baseline_path)
    found = parse(sys.stdin)

    new = sorted(found - allowed)
    stale = sorted(allowed - found)

    for code, file in new:
        print(f"error: new build warning {code} in {file}")
    for code, file in stale:
        print(f"error: baseline entry {code} in {file} no longer fires -- delete it")

    if new or stale:
        print(f"csharp warning gate FAILED: {len(new)} new, {len(stale)} stale (baseline: {baseline_path})")
        return 1
    print(f"csharp warning gate passed: {len(found)} warning(s), all baselined")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
