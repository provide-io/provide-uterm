#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Executable cross-language public-route session-lifecycle contract tests."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_PATH = REPO_ROOT / "spec/session_lifecycle_security_scenarios.json"
RUNNER_PATH = REPO_ROOT / "scripts/run_session_lifecycle_security_scenarios.py"
BACKENDS = {"python", "go", "csharp", "typescript", "cloudflare"}
STATUSES = {"served", "unsupported", "unserved"}
REQUIRED_CATEGORY_CARDINALITY = {
    "message fragmentation": 3,
    "browser quota accounting": 1,
    "configured governance": 3,
    "resume ownership and replay": 2,
    "non-owner hijack-step refusal": 1,
    # The owner-handoff and approval-expiry races from ead7567f. The runner
    # already classifies both operations; only this map was left behind.
    "ownership handoff": 1,
    "approval expiry": 1,
}
EXPECTED_FIELDS = {
    "route",
    "status_code",
    "error",
    "fragment_count",
    "accepted_connections",
    "rejected_connections",
    "quota_recovered",
    "delivered_payloads",
    "resume_succeeded",
    "ownership_restored",
    "replay_rejected",
    "non_owner_refused",
    "pre_final_actions",
    "post_final_actions",
    "oversized_refused",
    "setup_rollback_verified",
    "policy_decision",
    "signed_request",
    "competing_owner_preserved",
    # Owner-handoff and approval-expiry races, added to the contract by
    # ead7567f. The contract grew these five and this set did not, so the
    # shape assertion below has been failing ever since -- the drift is
    # one-way by design: the test pins the contract so an accidental field
    # cannot appear unnoticed, which is exactly what it caught.
    "approval_expired",
    "handoff_completed",
    "late_approval_refused",
    "stale_owner_refused",
    "successor_owner_accepted",
}


def _contract() -> dict[str, object]:
    return json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))


def _runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("session_lifecycle_security_scenario_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _valid_observations(runner: ModuleType, contract: dict[str, object], backend: str) -> list[dict[str, object]]:
    return [
        {
            "id": scenario["id"],
            "status": scenario["backends"][backend]["status"],
            **runner.expected_for(contract, scenario, backend),
        }
        for scenario in contract["scenarios"]
        if scenario["backends"][backend]["status"] != "unserved"
    ]


def test_contract_has_exact_categories_backend_cells_and_result_fields() -> None:
    runner = _runner()
    contract = _contract()

    assert contract["schema_version"] == 1
    assert set(contract["status_vocabulary"]) == STATUSES  # type: ignore[arg-type]
    assert set(contract["backends"]) == BACKENDS  # type: ignore[arg-type]
    scenarios = contract["scenarios"]
    assert isinstance(scenarios, list) and scenarios
    assert set(contract["result_defaults"]) == EXPECTED_FIELDS
    category_counts = dict.fromkeys(REQUIRED_CATEGORY_CARDINALITY, 0)
    for scenario in scenarios:
        category_counts[runner.semantic_category(scenario)] += 1
    assert category_counts == REQUIRED_CATEGORY_CARDINALITY
    for scenario in scenarios:
        assert set(scenario) == {"id", "input", "expected", "backends"}
        assert set(scenario["expected"]) <= EXPECTED_FIELDS
        assert set(scenario["backends"]) == BACKENDS
        for backend in BACKENDS:
            claim = scenario["backends"][backend]
            assert set(claim) == {"status", "expected"}
            assert claim["status"] in STATUSES
            assert set(claim["expected"]) <= EXPECTED_FIELDS
            assert set(runner.expected_for(contract, scenario, backend)) == EXPECTED_FIELDS
    assert runner.validate_contract(contract) == []
    claimed_backends = {
        backend
        for scenario in scenarios
        for backend, claim in scenario["backends"].items()
        if claim["status"] != "unserved"
    }
    assert claimed_backends <= set(runner.EXECUTABLE_BACKENDS)


def test_every_executable_backend_has_a_native_command() -> None:
    runner = _runner()

    for backend in runner.EXECUTABLE_BACKENDS:
        command, cwd = runner._command(REPO_ROOT, backend)
        assert command
        assert cwd.is_dir()


@pytest.mark.parametrize("category", sorted(REQUIRED_CATEGORY_CARDINALITY))
def test_validator_rejects_each_missing_semantic_category(category: str) -> None:
    runner = _runner()
    contract = _contract()
    contract["scenarios"] = [
        scenario for scenario in contract["scenarios"] if runner.semantic_category(scenario) != category
    ]

    assert any(category in error for error in runner.validate_contract(contract))


@pytest.mark.parametrize(
    ("category", "minimum"),
    sorted(REQUIRED_CATEGORY_CARDINALITY.items()),
)
def test_validator_enforces_required_category_cardinality(category: str, minimum: int) -> None:
    runner = _runner()
    contract = _contract()
    for index, scenario in enumerate(contract["scenarios"]):
        if runner.semantic_category(scenario) == category:
            del contract["scenarios"][index]
            break

    assert any(f"{category}: expected at least {minimum}" in error for error in runner.validate_contract(contract))


def test_validator_rejects_missing_backend_cell_and_unexpected_unsupported_claim() -> None:
    runner = _runner()
    contract = _contract()

    missing = copy.deepcopy(contract)
    del missing["scenarios"][0]["backends"]["go"]
    assert any("backend claims must be exact" in error for error in runner.validate_contract(missing))

    false_unsupported = copy.deepcopy(contract)
    false_unsupported["scenarios"][0]["backends"]["python"]["status"] = "unsupported"
    assert any("false capability status" in error for error in runner.validate_contract(false_unsupported))


def test_comparator_rejects_silent_skip_duplicate_missing_and_mismatch() -> None:
    runner = _runner()
    contract = _contract()
    valid = _valid_observations(runner, contract, "python")

    assert runner.compare_observations(contract, "python", valid) == []
    skipped = copy.deepcopy(valid)
    skipped[0]["skipped"] = True
    assert any(
        "skip is not executable evidence" in error for error in runner.compare_observations(contract, "python", skipped)
    )
    assert any("duplicate" in error for error in runner.compare_observations(contract, "python", [*valid, valid[0]]))
    assert any("missing" in error for error in runner.compare_observations(contract, "python", valid[1:]))
    mismatched = copy.deepcopy(valid)
    mismatched[0]["status_code"] = -1
    assert any("result mismatch" in error for error in runner.compare_observations(contract, "python", mismatched))


def test_comparator_requires_exact_normalized_observation_fields() -> None:
    runner = _runner()
    contract = _contract()
    valid = _valid_observations(runner, contract, "python")

    missing_nullable = copy.deepcopy(valid)
    del missing_nullable[0]["error"]
    assert any(
        "observation fields mismatch" in error
        for error in runner.compare_observations(contract, "python", missing_nullable)
    )

    unexpected = copy.deepcopy(valid)
    unexpected[0]["untracked_evidence"] = True
    assert any(
        "observation fields mismatch" in error for error in runner.compare_observations(contract, "python", unexpected)
    )


def test_adapter_timeout_failure_and_missing_output_are_hard_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = _runner()
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(_contract()), encoding="utf-8")

    def timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["adapter"], timeout=1)

    monkeypatch.setattr(runner.subprocess, "run", timeout)
    errors, observations = runner.collect_backend_observations(REPO_ROOT, contract_path, "python", timeout_s=1)
    assert observations == []
    assert any("timed out" in error for error in errors)

    failed = subprocess.CompletedProcess(["adapter"], returncode=7, stdout="native stdout", stderr="native stderr")
    monkeypatch.setattr(runner.subprocess, "run", lambda *_args, **_kwargs: failed)
    errors, _ = runner.collect_backend_observations(REPO_ROOT, contract_path, "python")
    assert any("native command failed (7)" in error for error in errors)

    passed_without_output = subprocess.CompletedProcess(["adapter"], returncode=0, stdout="", stderr="")
    monkeypatch.setattr(runner.subprocess, "run", lambda *_args, **_kwargs: passed_without_output)
    errors, _ = runner.collect_backend_observations(REPO_ROOT, contract_path, "python")
    assert any("produced no observation file" in error for error in errors)


def test_unserved_backend_is_not_launched(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner()
    contract = _contract()
    launched: list[str] = []
    monkeypatch.setattr(
        runner,
        "collect_backend_observations",
        lambda _root, _contract_path, backend, **_kwargs: (launched.append(backend), [])[1],
    )

    assert runner.run_backend(REPO_ROOT, SCENARIOS_PATH, contract, "typescript") == []
    assert launched == []


def test_default_paths_are_independent_of_callers_working_directory(tmp_path: Path) -> None:
    result = subprocess.run(
        ["python", str(RUNNER_PATH), "--validate-only"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "session-lifecycle contract validation passed"
