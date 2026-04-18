#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Run every package's test suite in sequence with its own coverage config.

Each workspace package defines its own 100% branch-coverage gate via its
``pyproject.toml``.  The root ``uv run pytest`` only covers the core and
Cloudflare packages (the only two in ``[tool.pytest.ini_options].testpaths``);
this wrapper also exercises server, platform/manager, and platform/pty so
contributors get a single command that mirrors what CI runs across jobs.

Exits non-zero on the first package whose tests fail, surfacing the raw pytest
output for that package. Pass through any extra args to every pytest invocation
(for example ``--no-cov -k name``).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

_PACKAGE_SUITES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("provide-terminal + provide-terminal-cloudflare (root pytest)", ()),
    ("provide-terminal-server", ("packages/provide-terminal-server/tests/",)),
    (
        "provide-terminal-platform/manager",
        ("packages/provide-terminal-platform/tests/manager/",),
    ),
    (
        "provide-terminal-platform/pty (no PAM/root)",
        (
            "packages/provide-terminal-platform/tests/pty/",
            "--ignore=packages/provide-terminal-platform/tests/pty/e2e",
            "--no-cov",
            "--timeout=10",
            "-o",
            "addopts=",
        ),
    ),
)


def _run(label: str, args: tuple[str, ...], passthrough: list[str]) -> int:
    print(f"\n=== {label} ===", flush=True)
    cmd = ["uv", "run", "pytest", "-q", *args, *passthrough]
    return subprocess.call(cmd, cwd=str(_REPO_ROOT))


def main() -> int:
    passthrough = sys.argv[1:]
    for label, args in _PACKAGE_SUITES:
        rc = _run(label, args, passthrough)
        if rc != 0:
            print(f"FAILED: {label} (exit {rc})", file=sys.stderr)
            return rc
    print("\nAll package test suites passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
