#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the version-consistency gate's per-package VERSION check.

The check is only worth having if it fails on the drift it claims to catch, so
these pin the asymmetry it encodes: a Python package's VERSION is its published
version and must equal the root's, while the Go and C# ports may cut ahead of
the workspace through their own workflow_dispatch publish route.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SCRIPT_PATH = _ROOT / "scripts" / "check_version_consistency.py"
_spec = importlib.util.spec_from_file_location("check_version_consistency", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
check_version_consistency = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_version_consistency)


def _package(root: Path, name: str, version: str, *, python: bool) -> Path:
    """Create a package directory with a VERSION, and a pyproject if Python."""
    directory = root / "packages" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    if python:
        (directory / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    return directory


def _drift(root: Path, version: str = "0.5.5", *, fix: bool = False) -> list[str]:
    return check_version_consistency.version_file_drift(root, version, fix=fix)


def test_a_python_package_must_match_the_root(tmp_path: Path) -> None:
    """Its VERSION is what setuptools publishes, so a mismatch ships a bad wheel."""
    _package(tmp_path, "provide-uterm-server", "0.5.4", python=True)

    drifted = _drift(tmp_path)

    assert len(drifted) == 1
    assert "provide-uterm-server/VERSION:1: 0.5.4" in drifted[0]


def test_a_python_package_ahead_of_the_root_also_drifts(tmp_path: Path) -> None:
    """Equality both ways: release.yml publishes all six together."""
    _package(tmp_path, "provide-uterm-server", "0.9.9", python=True)

    assert len(_drift(tmp_path)) == 1


def test_a_port_may_cut_ahead_of_the_workspace(tmp_path: Path) -> None:
    """Go and C# publish from their own VERSION on workflow_dispatch."""
    _package(tmp_path, "provide-uterm-go", "0.5.6", python=False)

    assert _drift(tmp_path) == []


def test_a_port_behind_the_root_is_reported(tmp_path: Path) -> None:
    """It would be republished at the root's version, overwriting what it cut."""
    _package(tmp_path, "provide-uterm-go", "0.5.4", python=False)

    drifted = _drift(tmp_path)

    assert len(drifted) == 1
    assert "never lag" in drifted[0]


def test_an_unparseable_version_is_reported(tmp_path: Path) -> None:
    """Neither equal nor comparable, so it cannot be judged as either."""
    _package(tmp_path, "provide-uterm-go", "0.5.6-rc1", python=False)

    drifted = _drift(tmp_path)

    assert len(drifted) == 1
    assert "not a dotted numeric version" in drifted[0]


def test_an_exact_match_is_silent(tmp_path: Path) -> None:
    _package(tmp_path, "provide-uterm-server", "0.5.5", python=True)
    _package(tmp_path, "provide-uterm-go", "0.5.5", python=False)

    assert _drift(tmp_path) == []


def test_fix_repins_a_drifted_package_and_leaves_a_port_that_cut_ahead(tmp_path: Path) -> None:
    """--fix must not drag a deliberately-cut port backwards."""
    _package(tmp_path, "provide-uterm-server", "0.5.4", python=True)
    _package(tmp_path, "provide-uterm-go", "0.5.6", python=False)

    _drift(tmp_path, fix=True)

    assert (tmp_path / "packages/provide-uterm-server/VERSION").read_text(encoding="utf-8") == "0.5.5\n"
    assert (tmp_path / "packages/provide-uterm-go/VERSION").read_text(encoding="utf-8") == "0.5.6\n"


def test_an_unparseable_version_is_never_rewritten(tmp_path: Path) -> None:
    """A value this check cannot read is one a human has to look at."""
    _package(tmp_path, "provide-uterm-go", "nightly", python=False)

    _drift(tmp_path, fix=True)

    assert (tmp_path / "packages/provide-uterm-go/VERSION").read_text(encoding="utf-8") == "nightly\n"


def test_the_real_repository_agrees_with_its_own_version_file() -> None:
    """The gate's own subject, so a drift on main fails here too."""
    version = (_ROOT / "VERSION").read_text(encoding="utf-8").strip()

    assert check_version_consistency.version_file_drift(_ROOT, version, fix=False) == []
