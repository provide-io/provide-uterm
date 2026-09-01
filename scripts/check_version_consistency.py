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

The npm workspaces carry the same version in their own manifests, and their
package-lock.json entries carry it again. Both are checked here for the reason
the VERSION files are: they were enforced only by
``packages/provide-uterm-ts/src/server/version-consistency.test.ts``, so a
Python-side change that never runs vitest could leave them behind with this gate
green.

Run with no arguments to report drift (this is what the quality gate does);
``--fix`` rewrites the drifted references in place. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

from repo_paths import submodule_dirs

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The TypeScript server's self-reported version. One declaration, one file, so
#: it is named rather than searched for -- and a rename is reported rather than
#: skipped, because a check that quietly stops looking is worse than no check.
SERVER_VERSION_FILE = Path("packages/provide-uterm-ts/src/server/bootstrap.ts")
SERVER_VERSION_PATTERN = re.compile(r'^(export const SERVER_VERSION = ")(?P<version>[^"]*)(";)$', re.M)


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


def version_file_drift(root: Path, version: str, *, fix: bool) -> list[str]:
    """Report (and optionally repin) each package's own VERSION file.

    A package's VERSION *is* its published version -- setuptools reads it for
    ``dynamic = ["version"]``, ci/tag_go_module.sh and ci/publish_nuget.sh read
    it for the tag and the NuGet package -- and release.yml publishes them
    together, so one that disagrees with the root ships under the wrong number.

    Equality, in both directions and for every package. That rule is not new
    here: packages/provide-uterm-ts/src/server/version-consistency.test.ts has
    enforced it, along with the npm manifests, the lock, SERVER_VERSION and the
    CHANGELOG heading, for longer than this file has existed. This is the same
    rule in the static gate, where a Python-side change that never runs vitest
    can still hit it -- and where ``--fix`` can repin it.

    Submodules are skipped: packages/provide-telemetry is released on its own
    cadence and its VERSION is not ours to keep in step.
    """
    drifted: list[str] = []
    foreign = submodule_dirs(root)
    for version_file in sorted(root.glob("packages/*/VERSION")):
        if version_file.parent.resolve() in foreign:
            continue
        declared = version_file.read_text(encoding="utf-8").strip()
        if declared == version:
            continue
        drifted.append(f"{version_file.relative_to(root)}:1: {declared} — expected {version}")
        if fix:
            version_file.write_text(f"{version}\n", encoding="utf-8")
    return drifted


def npm_manifest_drift(root: Path, version: str, *, fix: bool) -> list[str]:
    """Report (and optionally repin) each npm workspace's version and lock entry.

    The npm side is the other half of the same rule the VERSION files carry, and
    it was the half only ``npm-quality`` could see: a Python-side change that
    never runs vitest could leave the three manifests behind and the static gate
    would say every version reference agreed.

    ``--fix`` repins the manifests, which are hand-maintained, and deliberately
    does NOT touch package-lock.json. That file is generated, and rewriting an
    entry by hand is how a lock stops matching what npm would produce -- the
    drift is reported with the command that regenerates it instead. Repinning a
    manifest makes the lock disagree anyway, so a fix run ends by asking for it.
    """
    manifest_path = root / "package.json"
    if not manifest_path.is_file():
        return []
    workspaces = json.loads(manifest_path.read_text(encoding="utf-8")).get("workspaces") or []

    lock_path = root / "package-lock.json"
    locked: dict[str, dict[str, object]] = {}
    if lock_path.is_file():
        packages = json.loads(lock_path.read_text(encoding="utf-8")).get("packages")
        if isinstance(packages, dict):
            locked = packages

    drifted: list[str] = []
    for workspace in workspaces:
        workspace_manifest = root / str(workspace) / "package.json"
        if not workspace_manifest.is_file():
            continue
        text = workspace_manifest.read_text(encoding="utf-8")
        declared = json.loads(text).get("version")
        # A private package with no version is not a version reference at all.
        if isinstance(declared, str) and declared != version:
            rel = workspace_manifest.relative_to(root)
            drifted.append(f'{rel}: "version": "{declared}" — expected {version}')
            if fix:
                # Anchored on the top-level two-space key rather than the first
                # match: "version" also appears under dependencies, and a bare
                # count=1 substitution would repin whichever came first.
                workspace_manifest.write_text(
                    re.sub(
                        rf'^  "version": "{re.escape(declared)}"',
                        f'  "version": "{version}"',
                        text,
                        count=1,
                        flags=re.M,
                    ),
                    encoding="utf-8",
                )

        entry = locked.get(str(workspace))
        if isinstance(entry, dict):
            in_lock = entry.get("version")
            if isinstance(in_lock, str) and in_lock != version:
                drifted.append(
                    f"package-lock.json: {workspace} is locked at {in_lock} — expected {version}"
                    " (run `npm install --package-lock-only`)"
                )
    return drifted


def server_version_drift(root: Path, version: str, *, fix: bool) -> list[str]:
    """Report (and optionally repin) the TypeScript server's advertised version.

    It is what the server tells a client it is, so a stale literal misidentifies
    a running process -- and unlike the packages, nothing derives it from a file
    at build time.

    An absent file or a renamed constant is reported, not skipped. Three
    separate gates in this repo have silently excluded what they appeared to
    cover, and every one of them looked green while doing it; a check anchored
    on a single name has to say when the anchor moves.
    """
    path = root / SERVER_VERSION_FILE
    if not path.is_file():
        return [f"{SERVER_VERSION_FILE}: expected to declare SERVER_VERSION, but the file is missing"]

    text = path.read_text(encoding="utf-8")
    match = SERVER_VERSION_PATTERN.search(text)
    if match is None:
        return [f'{SERVER_VERSION_FILE}: no `export const SERVER_VERSION = "..."` — has it been renamed?']
    if match["version"] == version:
        return []

    if fix:
        path.write_text(
            SERVER_VERSION_PATTERN.sub(rf"\g<1>{version}\g<3>", text, count=1),
            encoding="utf-8",
        )
    line_no = text[: match.start()].count("\n") + 1
    return [f'{SERVER_VERSION_FILE}:{line_no}: SERVER_VERSION = "{match["version"]}" — expected {version}']


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
    drifted += npm_manifest_drift(REPO_ROOT, version, fix=args.fix)
    drifted += server_version_drift(REPO_ROOT, version, fix=args.fix)
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
