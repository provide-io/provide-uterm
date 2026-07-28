#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
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
    # C# build output and Go vendored sources: generated or third-party, so a
    # size cap on them measures nothing a reviewer can act on.
    "bin",
    "obj",
    "vendor",
    ".worktrees",
}

DEFAULT_SUFFIXES = (".py", ".cs", ".go", ".ts")


def _iter_source_files(roots: list[Path], suffixes: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix not in suffixes:
                continue
            if any(part in DEFAULT_EXCLUDE_PARTS for part in path.parts):
                continue
            if path.is_file():
                files.append(path)
    return files


def _line_count(path: Path) -> int:
    # Count physical lines to enforce a hard size cap.
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def find_loc_offenders(
    roots: list[Path], max_lines: int, suffixes: tuple[str, ...] = DEFAULT_SUFFIXES
) -> list[tuple[Path, int]]:
    offenders: list[tuple[Path, int]] = []
    for path in sorted(_iter_source_files(roots, suffixes)):
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
    parser = argparse.ArgumentParser(description="Fail if any source file exceeds a maximum line count.")
    parser.add_argument("--max-lines", type=int, default=777, help="Maximum allowed lines per source file.")
    parser.add_argument(
        "--suffixes",
        nargs="+",
        default=list(DEFAULT_SUFFIXES),
        help="File suffixes to scan. The cap applies to every language equally.",
    )
    parser.add_argument(
        "--roots",
        nargs="+",
        default=[
            "packages/provide-uterm/src",
            "packages/provide-uterm/tests",
            "scripts",
            "packages/provide-uterm-cloudflare/src",
            "packages/provide-uterm-cloudflare/tests",
            "packages/provide-uterm-csharp/src",
            "packages/provide-uterm-csharp/tests",
            "packages/provide-uterm-csharp/cmd",
            "packages/provide-uterm-go",
            "packages/provide-uterm-ts/src",
        ],
        help="Directories to scan.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Optional JSON ratchet baseline for known legacy files above the limit.",
    )
    args = parser.parse_args()

    roots = [Path(root) for root in args.roots]
    suffixes = tuple(s if s.startswith(".") else f".{s}" for s in args.suffixes)
    offenders = find_loc_offenders(roots, args.max_lines, suffixes)
    if args.baseline is None and not offenders:
        print(f"LOC check passed: no source file exceeds {args.max_lines} lines.")
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
            print(f"LOC check passed: no source file exceeds {args.max_lines} lines.")
            return 0

    print(f"LOC check failed: {len(offenders)} file(s) exceed {args.max_lines} lines.")
    for path, lines in offenders:
        print(f"  {path}: {lines}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
