#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from scripts.package_metadata import DEPENDENT_PACKAGES  # noqa: E402

_SCRIPT_PATH = _ROOT / "scripts" / "verify_package_artifacts.py"
_spec = importlib.util.spec_from_file_location("verify_package_artifacts", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
artifacts = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = artifacts
_spec.loader.exec_module(artifacts)


def test_published_packages_cover_workspace_python_distributions() -> None:
    assert [pkg.name for pkg in artifacts.PUBLISHED_PACKAGES] == [
        "provide-uterm",
        "provide-uterm-server",
        "provide-uterm-client",
        "provide-uterm-platform",
        "provide-uterm-cloudflare",
    ]


def test_entry_point_expectations_cover_cli_packages() -> None:
    by_name = {pkg.name: pkg.entry_points for pkg in artifacts.PUBLISHED_PACKAGES}
    assert by_name["provide-uterm"] == {}
    assert by_name["provide-uterm-server"] == {"uterm": "provide.uterm.cli:main"}
    assert by_name["provide-uterm-client"] == {"uterm-mcp": "provide.uterm.ai.cli:main"}
    assert by_name["provide-uterm-platform"] == {"uterm-manager": "provide.uterm.manager.cli:main"}
    assert by_name["provide-uterm-cloudflare"] == {"uterm-cf": "provide.uterm.cloudflare.cli:main"}


def test_required_package_data_includes_py_typed_and_server_frontend() -> None:
    by_name = {pkg.name: artifacts._required_members(pkg) for pkg in artifacts.PUBLISHED_PACKAGES}
    assert "provide/uterm/py.typed" in by_name["provide-uterm"]
    assert "provide/uterm/py.typed" in by_name["provide-uterm-server"]
    assert "provide/uterm/ai/py.typed" in by_name["provide-uterm-client"]
    assert "provide/uterm/pty/py.typed" in by_name["provide-uterm-platform"]
    assert "provide/uterm/cloudflare/py.typed" in by_name["provide-uterm-cloudflare"]
    assert any(member.startswith("provide/uterm/server/frontend/") for member in by_name["provide-uterm-server"])


def test_release_workflow_dependent_matrix_matches_package_metadata() -> None:
    workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release.yml"
    parsed = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    matrix = parsed["jobs"]["verify-dependents"]["strategy"]["matrix"]["package"]
    assert tuple(matrix) == DEPENDENT_PACKAGES
