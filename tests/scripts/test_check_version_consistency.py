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


def test_the_real_repository_agrees_with_its_own_version_file() -> None:
    """The gate's own subject, so a drift on main fails here too."""
    version = (_ROOT / "VERSION").read_text(encoding="utf-8").strip()

    assert check_version_consistency.version_file_drift(_ROOT, version, fix=False) == []
