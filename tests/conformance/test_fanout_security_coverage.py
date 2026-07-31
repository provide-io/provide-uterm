#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Validation tests for the cross-language fan-out security evidence map."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "spec/fanout_security_coverage.json"


def _load_validator() -> ModuleType:
    path = REPO_ROOT / "scripts/validate_fanout_security_coverage.py"
    spec = importlib.util.spec_from_file_location("fanout_security_coverage_validator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_repository_fanout_security_manifest_is_valid() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/validate_fanout_security_coverage.py"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_missing_applicable_behavior_is_rejected() -> None:
    validator = _load_validator()
    manifest = _manifest()
    del manifest["backends"]["go"]["coverage"]["partial_failures"]  # type: ignore[index]

    errors = validator.validate_manifest(REPO_ROOT, manifest)

    assert any("go: coverage must contain every required behavior" in error for error in errors)


def test_false_served_capability_claim_is_rejected() -> None:
    validator = _load_validator()
    manifest = _manifest()
    typescript = manifest["backends"]["typescript"]  # type: ignore[index]
    typescript["capabilities"]["delivery"] = "served"  # type: ignore[index]

    errors = validator.validate_manifest(REPO_ROOT, manifest)

    assert any("typescript.served_evidence" in error for error in errors)
    assert any("served delivery cannot claim component-only policy" in error for error in errors)


def test_internally_consistent_but_false_server_claim_is_rejected() -> None:
    validator = _load_validator()
    manifest = _manifest()
    typescript = manifest["backends"]["typescript"]  # type: ignore[index]
    typescript["capabilities"] = {"delivery": "served", "policy": "implemented"}  # type: ignore[index]
    typescript["served_evidence"] = copy.deepcopy(  # type: ignore[index]
        typescript["coverage"]["unknown_members_default_reject"]["evidence"]  # type: ignore[index]
    )
    for claim in typescript["coverage"].values():  # type: ignore[union-attr]
        claim["status"] = "covered"

    errors = validator.validate_manifest(REPO_ROOT, manifest)

    assert any("typescript: capabilities do not match repository support matrix" in error for error in errors)


def test_policy_capability_and_coverage_status_must_agree() -> None:
    validator = _load_validator()
    manifest = _manifest()
    manifest["backends"]["go"]["coverage"]["policy_deny"]["status"] = "covered"  # type: ignore[index]

    errors = validator.validate_manifest(REPO_ROOT, manifest)

    assert any("go.policy_deny" in error and "expected 'unsupported'" in error for error in errors)


def test_missing_test_file_and_identifier_are_rejected() -> None:
    validator = _load_validator()
    manifest = _manifest()
    bad_file = copy.deepcopy(manifest)
    bad_file["backends"]["python"]["coverage"]["partial_failures"]["evidence"][0]["file"] = (  # type: ignore[index]
        "packages/provide-uterm-server/tests/bridge/does_not_exist.py"
    )
    bad_identifier = copy.deepcopy(manifest)
    bad_identifier["backends"]["go"]["coverage"]["partial_failures"]["evidence"][0]["test"] = (  # type: ignore[index]
        "TestThatDoesNotExist"
    )

    file_errors = validator.validate_manifest(REPO_ROOT, bad_file)
    identifier_errors = validator.validate_manifest(REPO_ROOT, bad_identifier)

    assert any("missing test file" in error for error in file_errors)
    assert any("test declaration 'TestThatDoesNotExist' not found" in error for error in identifier_errors)


def test_evidence_requires_a_real_test_declaration_not_a_loose_substring() -> None:
    validator = _load_validator()
    manifest = _manifest()
    manifest["backends"]["typescript"]["coverage"]["partial_failures"]["evidence"][0]["test"] = "fanout"  # type: ignore[index]

    errors = validator.validate_manifest(REPO_ROOT, manifest)

    assert any("test declaration 'fanout' not found" in error for error in errors)
