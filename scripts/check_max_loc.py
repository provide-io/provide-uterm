#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

from __future__ import annotations

import argparse
import json
from pathlib import Path

DEFAULT_EXCLUDE_PARTS = {
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "mutants",
    "build",
    "dist",
    "node_modules",
    "python_modules",
}


def _iter_python_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if any(part in DEFAULT_EXCLUDE_PARTS for part in path.parts):
                continue
            if path.is_file():
                files.append(path)
    return files


def _line_count(path: Path) -> int:
    # Count physical lines to enforce a hard size cap.
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def find_loc_offenders(roots: list[Path], max_lines: int) -> list[tuple[Path, int]]:
    offenders: list[tuple[Path, int]] = []
    for path in sorted(_iter_python_files(roots)):
        lines = _line_count(path)
        if lines > max_lines:
            offenders.append((path, lines))
    return offenders


def _load_baseline(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    allow = data.get("allow_over_limit", {}) if isinstance(data, dict) else {}
    if not isinstance(allow, dict):
        return {}
    return {key: value for key, value in allow.items() if isinstance(key, str) and isinstance(value, int)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail if any Python file exceeds a maximum line count.")
    parser.add_argument("--max-lines", type=int, default=500, help="Maximum allowed lines per .py file.")
    parser.add_argument(
        "--roots",
        nargs="+",
        default=[
            "packages/provide-terminal/src",
            "packages/provide-terminal/tests",
            "scripts",
            "packages/provide-terminal-cloudflare/src",
            "packages/provide-terminal-cloudflare/tests",
        ],
        help="Directories to scan for Python files.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Optional JSON ratchet baseline for known legacy files above the limit.",
    )
    args = parser.parse_args()

    roots = [Path(root) for root in args.roots]
    offenders = find_loc_offenders(roots, args.max_lines)
    if args.baseline is None and not offenders:
        print(f"LOC check passed: no Python file exceeds {args.max_lines} lines.")
        return 0

    if args.baseline is not None:
        baseline = _load_baseline(args.baseline)
        new_offenders: list[tuple[Path, int]] = []
        for path, lines in offenders:
            key = str(path)
            allowed = baseline.get(key)
            if allowed is None or lines > allowed:
                new_offenders.append((path, lines))
        offenders = new_offenders
        if not offenders:
            print(f"LOC check passed: no Python file exceeds {args.max_lines} lines.")
            return 0

    print(f"LOC check failed: {len(offenders)} file(s) exceed {args.max_lines} lines.")
    for path, lines in offenders:
        print(f"  {path}: {lines}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
