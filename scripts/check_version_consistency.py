#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Keep every hand-written version reference agreeing with VERSION.

Every published package derives its own version from its ``VERSION`` file
(``dynamic = ["version"]`` + ``[tool.setuptools.dynamic]``), so a package can
never disagree with its own file about what it is -- but that file could
disagree with the ROOT, and until it was checked here nothing compared the two.
A Python package whose VERSION lagged would have shipped a wheel under the wrong
number with the gate green. The floors those packages
declare on EACH OTHER are the exception: they are hand-written literals, and
nothing re-reads them when the release bumps. They drifted to ``>=0.5.0`` while
the workspace shipped ``0.5.5`` -- five releases with a floor that let pip
resolve a sibling from a different release line than the one it was tested with.

Dependabot noticed one at a time (PRs #83/#84 raised two of nineteen to
``>=0.5.1``, still four releases behind on the day they merged), which is a
treadmill rather than a fix: the floors are derivable from VERSION, so deriving
them is what stops the drift.

The workspace root is the other hand-written copy. It declares
``version = "0.5.5"`` under ``[project]`` and has no ``[build-system]``, so
unlike the published packages it cannot derive that from VERSION -- and unlike
them, nothing publishes it either, so a drift there would never surface as a
bad artifact. It would just sit in the file disagreeing with the release.

Run with no arguments to report drift (this is what the quality gate does);
``--fix`` rewrites the drifted references in place. Stdlib only.
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


def numeric_version(text: str) -> tuple[int, ...] | None:
    """Parse a dotted numeric version, or None when it is not one."""
    parts = text.split(".")
    if not text or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def version_file_drift(root: Path, version: str, *, fix: bool) -> list[str]:
    """Report (and optionally repin) each package's own VERSION file.

    A Python package's VERSION *is* its published version -- setuptools reads
    it for ``dynamic = ["version"]`` -- and release.yml publishes all six
    together, so one that disagrees with the root ships a wheel under the wrong
    number. Those are held equal.

    The Go and C# ports are not: both have a workflow_dispatch route that
    publishes from their own VERSION with the release-tag guard skipped, which
    is how a port cuts a release between workspace ones. So a port is allowed
    to be AHEAD of the root and only reported when it lags -- a port behind the
    root would be re-published at the root's version on the next release and
    silently overwrite what it cut.
    """
    drifted: list[str] = []
    expected = numeric_version(version)
    for version_file in sorted(root.glob("packages/*/VERSION")):
        rel = version_file.relative_to(root)
        declared = version_file.read_text(encoding="utf-8").strip()
        if declared == version:
            continue

        independent = not (version_file.parent / "pyproject.toml").is_file()
        found = numeric_version(declared)
        if found is None:
            drifted.append(f'{rel}:1: "{declared}" — not a dotted numeric version')
            continue
        if independent and expected is not None and found > expected:
            # A port that cut its own release. Deliberate, and the reason this
            # check cannot simply demand equality everywhere.
            continue

        lagging = " (a port may cut ahead of the workspace, never lag)" if independent else ""
        drifted.append(f"{rel}:1: {declared} — expected {version}{lagging}")
        if fix:
            version_file.write_text(f"{version}\n", encoding="utf-8")
    return drifted


def root_version_drift(pyproject: Path, version: str, *, fix: bool) -> list[str]:
    """Report (and optionally repin) the workspace root's own literal version.

    ``[project]`` is the first table in the file, so the first top-level
    ``version = "..."`` line is its own -- anchoring on that rather than on the
    value keeps this from rewriting a version literal belonging to some
    ``[tool.*]`` table further down.
    """
    text = pyproject.read_text(encoding="utf-8")
    declared = tomllib.loads(text).get("project", {}).get("version")
    if declared is None or declared == version:
        return []

    if fix:
        pyproject.write_text(
            re.sub(rf'^version = "{re.escape(declared)}"$', f'version = "{version}"', text, count=1, flags=re.M),
            encoding="utf-8",
        )
    line_no = next(
        (i for i, line in enumerate(text.splitlines(), start=1) if line == f'version = "{declared}"'),
        0,
    )
    return [f'{pyproject.relative_to(REPO_ROOT)}:{line_no}: [project] version = "{declared}" — expected {version}']


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
    root_pyproject = REPO_ROOT / "pyproject.toml"
    scanned = [root_pyproject, *sorted(members.values())]

    drifted: list[str] = []
    drifted += root_version_drift(root_pyproject, version, fix=args.fix)
    drifted += version_file_drift(REPO_ROOT, version, fix=args.fix)
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
        print(f"version consistency: every version reference agrees with VERSION ({version})")
        return 0

    if args.fix:
        print(f"version consistency: repinned {len(drifted)} reference(s) to {version}")
        return 0

    print(f"version consistency: {len(drifted)} reference(s) drifted from VERSION ({version}):")
    for item in drifted:
        print(f"  {item}")
    print("\nRun `uv run python scripts/check_version_consistency.py --fix` to repin.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
