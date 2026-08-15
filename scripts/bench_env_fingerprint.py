#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Record the environment a benchmark ran in, so two results can be compared.

A cross-language benchmark number is meaningless on its own: the same command on
a laptop on battery and on a CI runner differ by more than most optimisations
do. Every result file this repo keeps is written next to a fingerprint, and a
comparison that spans two different fingerprints is a comparison of machines,
not of code.

Deliberately cheap and dependency-free -- it runs before every benchmark, so it
must not itself perturb the measurement.

Usage:
    uv run python scripts/bench_env_fingerprint.py [--output PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# Toolchains whose version changes the numbers. Recorded when present; a missing
# toolchain is recorded as absent rather than skipped, because "go was not
# installed" explains a missing Go row in the results.
TOOLCHAINS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("go", ("go", "version")),
    ("dotnet", ("dotnet", "--version")),
    ("node", ("node", "--version")),
)


def _capture(command: tuple[str, ...]) -> str | None:
    if shutil.which(command[0]) is None:
        return None
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _git_revision() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def _git_dirty() -> bool | None:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def fingerprint() -> dict[str, Any]:
    """Everything that plausibly moves a benchmark number."""
    return {
        "git": {
            "revision": _git_revision(),
            # A dirty tree is the single most common reason a "regression"
            # cannot be reproduced later.
            "dirty": _git_dirty(),
        },
        "host": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
        },
        "python": {
            "version": sys.version.split()[0],
            "implementation": platform.python_implementation(),
        },
        "toolchains": {name: _capture(command) for name, command in TOOLCHAINS},
        "ci": bool(os.environ.get("CI")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write JSON here instead of stdout.")
    args = parser.parse_args(argv)

    payload = json.dumps(fingerprint(), indent=2, sort_keys=True)
    if args.output is None:
        print(payload)
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload + "\n", encoding="utf-8")
    print(f"benchmark environment fingerprint written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
