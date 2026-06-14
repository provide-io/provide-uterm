#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Verify installed package imports and console scripts from release metadata."""

from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.package_metadata import PUBLISHED_PACKAGES  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: verify_installed_package.py <package>\n")
        return 2

    package_name = argv[1]
    by_name = {package.name: package for package in PUBLISHED_PACKAGES}
    package = by_name.get(package_name)
    if package is None:
        known = ", ".join(sorted(by_name))
        sys.stderr.write(f"unknown package: {package_name}; expected one of: {known}\n")
        return 2

    for import_name in package.import_names:
        importlib.import_module(import_name)

    missing = [name for name in package.entry_points if shutil.which(name) is None]
    if missing:
        sys.stderr.write(f"missing console script(s): {', '.join(missing)}\n")
        return 1

    print(f"installed package verification passed: {package_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
