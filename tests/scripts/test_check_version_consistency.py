#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the version-consistency gate's per-package VERSION check.

The rule is equality, in both directions and for every package, matching what
``packages/provide-uterm-ts/src/server/version-consistency.test.ts`` has always
enforced. These exist because the two are separate implementations of one rule,
and a rule implemented twice is a rule that can disagree with itself -- which is
exactly what happened when this check first shipped allowing a port to sit ahead
of the workspace while the npm-side one demanded equality.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# The script imports its sibling repo_paths, which only resolves when scripts/
# is importable -- true when it runs as a script, not when a test loads it.
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_SCRIPT_PATH = _SCRIPTS / "check_version_consistency.py"
_spec = importlib.util.spec_from_file_location("check_version_consistency", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
check_version_consistency = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_version_consistency)


def _package(root: Path, name: str, version: str) -> Path:
    directory = root / "packages" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    return directory


def _drift(root: Path, version: str = "0.5.6", *, fix: bool = False) -> list[str]:
    return check_version_consistency.version_file_drift(root, version, fix=fix)


def test_a_package_behind_the_root_is_reported(tmp_path: Path) -> None:
    """Its VERSION is what setuptools publishes, so a mismatch ships a bad wheel."""
    _package(tmp_path, "provide-uterm-server", "0.5.5")

    drifted = _drift(tmp_path)

    assert len(drifted) == 1
    assert "provide-uterm-server/VERSION:1: 0.5.5 — expected 0.5.6" in drifted[0]


def test_a_package_ahead_of_the_root_is_reported_too(tmp_path: Path) -> None:
    """Equality both ways.

    Tagging the Go port at 0.5.6 against a 0.5.5 root is what turned this from a
    hypothetical into a red build; a check that tolerates "ahead" would have let
    the workspace and its published module disagree indefinitely.
    """
    _package(tmp_path, "provide-uterm-go", "0.5.7")

    assert len(_drift(tmp_path)) == 1


def test_every_package_is_measured_not_just_the_python_ones(tmp_path: Path) -> None:
    """The Go and C# ports publish from their VERSION too — a tag and a NuGet package."""
    _package(tmp_path, "provide-uterm-go", "0.5.5")
    _package(tmp_path, "provide-uterm-csharp", "0.5.5")
    _package(tmp_path, "provide-uterm-server", "0.5.6")

    assert len(_drift(tmp_path)) == 2


def test_an_exact_match_is_silent(tmp_path: Path) -> None:
    _package(tmp_path, "provide-uterm-server", "0.5.6")
    _package(tmp_path, "provide-uterm-go", "0.5.6")

    assert _drift(tmp_path) == []


def test_a_submodule_is_left_alone(tmp_path: Path) -> None:
    """provide-telemetry is released on its own cadence; its VERSION is not ours."""
    _package(tmp_path, "provide-telemetry", "9.9.9")
    (tmp_path / ".gitmodules").write_text(
        '[submodule "packages/provide-telemetry"]\n\tpath = packages/provide-telemetry\n',
        encoding="utf-8",
    )

    assert _drift(tmp_path) == []


def test_fix_repins_every_drifted_package(tmp_path: Path) -> None:
    _package(tmp_path, "provide-uterm-server", "0.5.5")
    _package(tmp_path, "provide-uterm-go", "0.5.7")

    _drift(tmp_path, fix=True)

    assert (tmp_path / "packages/provide-uterm-server/VERSION").read_text(encoding="utf-8") == "0.5.6\n"
    assert (tmp_path / "packages/provide-uterm-go/VERSION").read_text(encoding="utf-8") == "0.5.6\n"


def test_fix_leaves_a_submodule_untouched(tmp_path: Path) -> None:
    _package(tmp_path, "provide-telemetry", "9.9.9")
    (tmp_path / ".gitmodules").write_text(
        '[submodule "packages/provide-telemetry"]\n\tpath = packages/provide-telemetry\n',
        encoding="utf-8",
    )

    _drift(tmp_path, fix=True)

    assert (tmp_path / "packages/provide-telemetry/VERSION").read_text(encoding="utf-8") == "9.9.9\n"


def _npm(root: Path, workspaces: dict[str, str | None], locked: dict[str, str] | None = None) -> None:
    """Write a root manifest, its workspace manifests, and a lock."""
    (root / "package.json").write_text(
        json.dumps({"name": "root", "workspaces": list(workspaces)}, indent=2) + "\n", encoding="utf-8"
    )
    for workspace, version in workspaces.items():
        directory = root / workspace
        directory.mkdir(parents=True, exist_ok=True)
        body = {"name": workspace.rsplit("/", 1)[-1]}
        if version is not None:
            body["version"] = version
        (directory / "package.json").write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    if locked is not None:
        packages = {workspace: {"version": version} for workspace, version in locked.items()}
        (root / "package-lock.json").write_text(
            json.dumps({"name": "root", "packages": packages}, indent=2) + "\n", encoding="utf-8"
        )


def _npm_drift(root: Path, version: str = "0.5.6", *, fix: bool = False) -> list[str]:
    return check_version_consistency.npm_manifest_drift(root, version, fix=fix)


def test_an_npm_workspace_behind_the_root_is_reported(tmp_path: Path) -> None:
    """The half only npm-quality could see: vitest never runs in the static gate."""
    _npm(tmp_path, {"packages/provide-uterm-ts": "0.5.5"})

    drifted = _npm_drift(tmp_path)

    assert len(drifted) == 1
    assert "provide-uterm-ts/package.json" in drifted[0]


def test_a_stale_lock_entry_is_reported_with_the_command_that_regenerates_it(tmp_path: Path) -> None:
    _npm(tmp_path, {"packages/a": "0.5.6"}, locked={"packages/a": "0.5.4"})

    drifted = _npm_drift(tmp_path)

    assert len(drifted) == 1
    assert "npm install --package-lock-only" in drifted[0]


def test_a_workspace_without_a_version_is_not_a_version_reference(tmp_path: Path) -> None:
    """A private package that declares none has nothing to disagree with."""
    _npm(tmp_path, {"packages/private": None})

    assert _npm_drift(tmp_path) == []


def test_a_workspace_whose_manifest_is_absent_is_skipped(tmp_path: Path) -> None:
    _npm(tmp_path, {"packages/a": "0.5.6"})
    (tmp_path / "packages/a/package.json").unlink()

    assert _npm_drift(tmp_path) == []


def test_a_repository_with_no_npm_workspaces_is_silent(tmp_path: Path) -> None:
    assert _npm_drift(tmp_path) == []


def test_fix_repins_a_manifest_but_never_the_lock(tmp_path: Path) -> None:
    """The lock is generated; hand-editing it is how it stops matching npm."""
    _npm(tmp_path, {"packages/a": "0.5.5"}, locked={"packages/a": "0.5.5"})

    _npm_drift(tmp_path, fix=True)

    assert json.loads((tmp_path / "packages/a/package.json").read_text())["version"] == "0.5.6"
    lock = json.loads((tmp_path / "package-lock.json").read_text())
    assert lock["packages"]["packages/a"]["version"] == "0.5.5"


def test_fix_repins_the_package_not_a_dependency_of_the_same_version(tmp_path: Path) -> None:
    """A dependency can carry the same version string, so the substitution is anchored."""
    directory = tmp_path / "packages/a"
    directory.mkdir(parents=True)
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "root", "workspaces": ["packages/a"]}, indent=2), encoding="utf-8"
    )
    (directory / "package.json").write_text(
        '{\n  "name": "a",\n  "version": "0.5.5",\n  "dependencies": {\n    "left-pad": {\n'
        '      "version": "0.5.5"\n    }\n  }\n}\n',
        encoding="utf-8",
    )

    _npm_drift(tmp_path, fix=True)

    written = (directory / "package.json").read_text(encoding="utf-8")
    assert '  "version": "0.5.6"' in written
    assert '      "version": "0.5.5"' in written


def _bootstrap(root: Path, body: str) -> Path:
    path = root / check_version_consistency.SERVER_VERSION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _server_drift(root: Path, version: str = "0.5.6", *, fix: bool = False) -> list[str]:
    return check_version_consistency.server_version_drift(root, version, fix=fix)


def test_a_stale_server_version_is_reported(tmp_path: Path) -> None:
    """It is what the server tells a client it is, so a stale literal lies about it."""
    _bootstrap(tmp_path, 'export const SERVER_VERSION = "0.5.4";\n')

    drifted = _server_drift(tmp_path)

    assert len(drifted) == 1
    assert 'SERVER_VERSION = "0.5.4" — expected 0.5.6' in drifted[0]


def test_a_matching_server_version_is_silent(tmp_path: Path) -> None:
    _bootstrap(tmp_path, 'export const SERVER_VERSION = "0.5.6";\n')

    assert _server_drift(tmp_path) == []


def test_a_renamed_constant_is_reported_not_skipped(tmp_path: Path) -> None:
    """The anchor moving must fail loudly.

    Three gates in this repo have silently excluded what they appeared to cover
    -- conformance tests outside the testpaths, package VERSIONs outside the
    version check, root tests outside the ruff scope -- and each looked green
    while doing it.
    """
    _bootstrap(tmp_path, 'export const SERVER_BUILD = "0.5.6";\n')

    drifted = _server_drift(tmp_path)

    assert len(drifted) == 1
    assert "has it been renamed?" in drifted[0]


def test_a_missing_bootstrap_is_reported_not_skipped(tmp_path: Path) -> None:
    drifted = _server_drift(tmp_path)

    assert len(drifted) == 1
    assert "the file is missing" in drifted[0]


def test_fix_repins_only_the_declaration(tmp_path: Path) -> None:
    """The literal appears once; its other uses reference the constant."""
    _bootstrap(
        tmp_path,
        'export const SERVER_VERSION = "0.5.4";\nconst pinned = { peer: "0.5.4" };\n',
    )

    _server_drift(tmp_path, fix=True)

    written = (tmp_path / check_version_consistency.SERVER_VERSION_FILE).read_text(encoding="utf-8")
    assert 'export const SERVER_VERSION = "0.5.6";' in written
    assert 'peer: "0.5.4"' in written


def test_the_real_repository_agrees_with_its_server_version() -> None:
    version = (_ROOT / "VERSION").read_text(encoding="utf-8").strip()

    assert check_version_consistency.server_version_drift(_ROOT, version, fix=False) == []


def test_the_real_repository_agrees_with_its_npm_manifests() -> None:
    version = (_ROOT / "VERSION").read_text(encoding="utf-8").strip()

    assert check_version_consistency.npm_manifest_drift(_ROOT, version, fix=False) == []


def test_the_real_repository_agrees_with_its_own_version_file() -> None:
    """The gate's own subject, so a drift on main fails here too."""
    version = (_ROOT / "VERSION").read_text(encoding="utf-8").strip()

    assert check_version_consistency.version_file_drift(_ROOT, version, fix=False) == []
