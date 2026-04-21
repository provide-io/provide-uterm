#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def find_noncompliant_files(root: Path, skip_globs: tuple[str, ...]) -> list[Path]:
    from spdx_headers import find_python_files, has_canonical_header

    noncompliant: list[Path] = []
    for path in find_python_files(root, skip_globs=skip_globs):
        text = path.read_text(encoding="utf-8")
        if not has_canonical_header(text):
            noncompliant.append(path)
    return noncompliant


def main() -> int:
    parser = argparse.ArgumentParser(description="Check canonical Python SPDX headers.")
    parser.add_argument("--root", type=Path, default=Path())
    parser.add_argument(
        "--skip-glob",
        action="append",
        default=[
            "**/__init__.py",
            "**/.venv-workers/**",
            "packages/provide-terminal-cloudflare/**",
            "scripts/*.py",
            "*/.venv-workers/*",
            "*/packages/provide-terminal-cloudflare/*",
            "*/scripts/*.py",
        ],
        help="Glob for files to skip (repeatable).",
    )
    args = parser.parse_args()

    offenders = find_noncompliant_files(args.root.resolve(), tuple(args.skip_glob))
    if offenders:
        print(f"SPDX header check failed: {len(offenders)} file(s) are noncompliant.")
        for path in offenders:
            print(f"  {path}")
        print("Run: uv run python scripts/normalize_spdx_headers.py")
        return 1
    print("SPDX header check passed: all Python files are compliant.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
