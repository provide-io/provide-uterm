#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Keep intra-workspace dependency floors pinned to the version being shipped.

Every published package derives its own version from its ``VERSION`` file
(``dynamic = ["version"]`` + ``[tool.setuptools.dynamic]``), so a package can
never disagree with the release about what it is. The floors those packages
declare on EACH OTHER are the exception: they are hand-written literals, and
nothing re-reads them when the release bumps. They drifted to ``>=0.5.0`` while
the workspace shipped ``0.5.5`` -- five releases with a floor that let pip
resolve a sibling from a different release line than the one it was tested with.

Dependabot noticed one at a time (PRs #83/#84 raised two of nineteen to
``>=0.5.1``, still four releases behind on the day they merged), which is a
treadmill rather than a fix: the floors are derivable from VERSION, so deriving
them is what stops the drift.

Run with no arguments to report drift (this is what the quality gate does);
``--fix`` rewrites the floors in place. Stdlib only.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def workspace_members(root: Path) -> dict[str, Path]:
    """Map each workspace package's distribution name to its pyproject."""
    members: dict[str, Path] = {}
    for pyproject in sorted(root.glob("packages/*/pyproject.toml")):
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        name = data.get("project", {}).get("name")
        if name:
            members[name] = pyproject
    return members


def floor_pattern(names: list[str]) -> re.Pattern[str]:
    """Match ``name[extras]>=X.Y.Z`` for any workspace package.

    Longest name first so ``provide-uterm`` cannot claim the prefix of
    ``provide-uterm-server`` -- and because a match must be followed by ``[``
    or ``>=``, a shorter alternative fails on the ``-`` and backtracks anyway.
    """
    alternation = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(rf"(?P<name>{alternation})(?P<extras>\[[^\]]*\])?>=(?P<floor>\d+(?:\.\d+)*)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="rewrite drifted floors in place")
    args = parser.parse_args()

    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    members = workspace_members(REPO_ROOT)
    if not members:
        print("no workspace packages found — is this the repo root?", file=sys.stderr)
        return 2

    pattern = floor_pattern(list(members))
    scanned = [REPO_ROOT / "pyproject.toml", *sorted(members.values())]

    drifted: list[str] = []
    for pyproject in scanned:
        original = pyproject.read_text(encoding="utf-8")
        for line_no, line in enumerate(original.splitlines(), start=1):
            for match in pattern.finditer(line):
                if match["floor"] != version:
                    rel = pyproject.relative_to(REPO_ROOT)
                    drifted.append(f"{rel}:{line_no}: {match[0]} — expected >={version}")

        if args.fix:
            updated = pattern.sub(lambda m: f"{m['name']}{m['extras'] or ''}>={version}", original)
            if updated != original:
                pyproject.write_text(updated, encoding="utf-8")

    if not drifted:
        print(f"version floors: all intra-workspace floors pinned to {version}")
        return 0

    if args.fix:
        print(f"version floors: repinned {len(drifted)} floor(s) to {version}")
        return 0

    print(f"version floors: {len(drifted)} floor(s) drifted from VERSION ({version}):")
    for item in drifted:
        print(f"  {item}")
    print("\nRun `uv run python scripts/check_version_floors.py --fix` to repin.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
