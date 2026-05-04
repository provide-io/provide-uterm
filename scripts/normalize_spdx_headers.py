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


def normalize_headers(root: Path, skip_globs: tuple[str, ...]) -> list[Path]:
    from spdx_headers import find_python_files, normalize_python_text

    changed: list[Path] = []
    for path in find_python_files(root, skip_globs=skip_globs):
        original = path.read_text(encoding="utf-8")
        normalized = normalize_python_text(original)
        if normalized != original:
            path.write_text(normalized, encoding="utf-8")
            changed.append(path)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize Python SPDX headers.")
    parser.add_argument("--root", type=Path, default=Path())
    parser.add_argument(
        "--skip-glob",
        action="append",
        default=[
            "**/__init__.py",
            "**/.venv-workers/**",
            "packages/provide-uterm-cloudflare/**",
            "scripts/*.py",
            "*/.venv-workers/*",
            "*/packages/provide-uterm-cloudflare/*",
            "*/scripts/*.py",
        ],
        help="Glob for files to skip (repeatable).",
    )
    args = parser.parse_args()

    changed = normalize_headers(args.root.resolve(), tuple(args.skip_glob))
    if changed:
        print(f"normalized SPDX headers in {len(changed)} file(s):")
        for path in changed:
            print(f"  {path}")
    else:
        print("all Python SPDX headers already normalized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
