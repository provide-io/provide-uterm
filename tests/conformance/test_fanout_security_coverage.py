#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Executable cross-language fan-out security contract tests."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_PATH = REPO_ROOT / "spec/fanout_security_scenarios.json"
RUNNER_PATH = REPO_ROOT / "scripts/run_fanout_security_scenarios.py"

server_impl = os.environ.get("SERVER_IMPL")
BACKENDS = {server_impl} if server_impl else {"python", "go", "csharp", "typescript"}

STATUSES = {"execute", "unsupported_fail_closed", "component_execute", "unserved"}
REQUIRED_CATEGORIES = {
    "authentication refusal",
    "viewer refusal",
    "strict dormant-member rejection",
    "permissive dormant-member admission",
    "current authorization revocation",
    "group grant non-bypass",
    "partial member failure",
    "policy deny",
    "policy hold and release",
    "missing authorization dependencies",
    "immediate output capture",
    "store read isolation",
    "store atomic update",
    "total response deadline",
    "registered member read refusal",
    "positional member refusal ordering",
    "controller cannot widen member access",
    "create authorization wiring refusal",
}
EXPECTED_FIELDS = {
    "status_code",
    "error",
    "approval_required",
    "approval_id",
    "command",
    "delivered_workers",
    "observer_notifications",
    "failed_members",
    "output",
}


def _contract() -> dict[str, object]:
    return json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))


def _runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fanout_security_scenario_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_contract_contains_semantic_inputs_expectations_and_exact_status_matrix() -> None:
    contract = _contract()

    assert contract["schema_version"] == 1
    assert set(contract["backends"]) == BACKENDS  # type: ignore[arg-type]
    scenarios = contract["scenarios"]
    assert isinstance(scenarios, list) and scenarios
    for scenario in scenarios:
        assert set(scenario) >= {"id", "input", "expected", "backends"}
        assert set(scenario["expected"]) == EXPECTED_FIELDS
        assert set(scenario["backends"]) == BACKENDS
        for backend in BACKENDS:
            claim = scenario["backends"][backend]
            assert set(claim) == {"status", "expected"}
            assert claim["status"] in STATUSES
            assert set(claim["expected"]) <= EXPECTED_FIELDS
            assert set(scenario["expected"] | claim["expected"]) == EXPECTED_FIELDS
        assert "file" not in scenario and "test" not in scenario
        assert set(scenario["input"]) >= {
            "surface",
            "operation",
            "actor",
            "group",
            "visibility",
            "policy",
            "workers",
            "command",
        }


def test_real_runner_executes_and_compares_native_observations() -> None:
    result = subprocess.run(
        [sys.executable, str(RUNNER_PATH), "--root", str(REPO_ROOT)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_comparator_rejects_missing_extra_skip_and_mismatched_results() -> None:
    runner = _runner()
    contract = _contract()
    backend = "python"
    expected_ids = runner.applicable_ids(contract, backend)
    valid = [
        {
            "id": scenario["id"],
            "status": scenario["backends"][backend]["status"],
            **runner.expected_for(scenario, backend),
        }
        for scenario in contract["scenarios"]
        if scenario["id"] in expected_ids
    ]

    assert runner.compare_observations(contract, backend, valid) == []
    assert any("missing" in error for error in runner.compare_observations(contract, backend, valid[1:]))
    assert any(
        "extra" in error
        for error in runner.compare_observations(contract, backend, [*valid, {**valid[0], "id": "not-a-case"}])
    )
    assert any(
        "skip" in error
        for error in runner.compare_observations(contract, backend, [{**valid[0], "skipped": True}, *valid[1:]])
    )
    assert any(
        "mismatch" in error
        for error in runner.compare_observations(
            contract, backend, [{**valid[0], "delivered_workers": ["wrong"]}, *valid[1:]]
        )
    )


def test_false_typescript_server_capability_claim_is_rejected() -> None:
    runner = _runner()
    contract = _contract()
    contract["backends"]["typescript"]["surface"] = "server"
    contract["backends"]["typescript"]["advertised"] = True

    errors = runner.validate_contract(contract)

    assert any("typescript" in error and "unserved component" in error for error in errors)


def test_scenario_ids_are_opaque_and_statuses_derive_from_semantic_inputs() -> None:
    runner = _runner()
    contract = copy.deepcopy(_contract())
    for index, scenario in enumerate(contract["scenarios"]):
        scenario["id"] = f"opaque-{index}"

    assert runner.validate_contract(contract) == []

    governed = next(scenario for scenario in contract["scenarios"] if scenario["input"]["policy"]["action"] == "deny")
    governed["backends"]["go"]["status"] = "execute"
    assert any("false capability status" in error for error in runner.validate_contract(contract))


def _categories(scenario: dict[str, object]) -> set[str]:
    input_data = scenario["input"]
    actor = input_data["actor"]
    group = input_data["group"]
    visibility = input_data["visibility"]
    policy = input_data["policy"]
    workers = input_data["workers"]
    operation = input_data["operation"]
    categories: set[str] = set()
    if not actor["authenticated"]:
        categories.add("authentication refusal")
    if actor["authenticated"] and "admin" not in actor["roles"]:
        categories.add("viewer refusal")
    if operation == "create" and not group["allow_unknown_members"]:
        categories.add("strict dormant-member rejection")
    if operation == "create" and group["allow_unknown_members"]:
        categories.add("permissive dormant-member admission")
    if visibility["revoke_before_send"] and not group["grants"]:
        categories.add("current authorization revocation")
    if visibility["revoke_before_send"] and group["grants"]:
        categories.add("group grant non-bypass")
    if (
        operation == "send"
        and not visibility["revoke_before_send"]
        and set(workers["accepted_members"]) < set(group["members"])
    ):
        categories.add("partial member failure")
    if policy["action"] == "deny":
        categories.add("policy deny")
    if policy["action"] == "hold_release":
        categories.add("policy hold and release")
    if input_data.get("omit_authorizers") is True:
        categories.add("missing authorization dependencies")
    if "immediate" in workers["immediate_output"].values():
        categories.add("immediate output capture")
    if operation == "store_read_isolation":
        categories.add("store read isolation")
    if operation == "store_atomic_update":
        categories.add("store atomic update")
    if operation == "create" and visibility.get("registered_members") and not visibility["readable_members"]:
        categories.add("registered member read refusal")
    if operation == "create" and len(group["members"]) > 1:
        categories.add("positional member refusal ordering")
    if operation == "create" and visibility.get("controller_readable_members"):
        categories.add("controller cannot widen member access")
    if operation == "create" and input_data.get("omit_authorizers") is True:
        categories.add("create authorization wiring refusal")
    if workers.get("continuous_output") is True and input_data.get("max_response_ms"):
        categories.add("total response deadline")
    return categories


@pytest.mark.parametrize("category", sorted(REQUIRED_CATEGORIES))
def test_contract_rejects_removal_of_each_required_semantic_category(category: str) -> None:
    runner = _runner()
    contract = copy.deepcopy(_contract())
    matching = [scenario for scenario in contract["scenarios"] if category in _categories(scenario)]
    assert matching
    contract["scenarios"] = [scenario for scenario in contract["scenarios"] if category not in _categories(scenario)]

    errors = runner.validate_contract(contract)

    assert any(category in error for error in errors)


def test_native_command_failure_is_not_reported_as_coverage() -> None:
    runner = _runner()

    errors = runner.command_errors("python", subprocess.CompletedProcess(["bad"], 2, "", "boom"))

    assert any("command failed" in error and "boom" in error for error in errors)


def test_adapter_timeout_is_a_hard_failure_without_partial_observations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = _runner()
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(_contract()), encoding="utf-8")

    def timeout(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] == 1
        raise subprocess.TimeoutExpired(
            ["adapter"],
            timeout=1,
            output='[{"id":"partial"}]',
            stderr="still running",
        )

    monkeypatch.setattr(runner.subprocess, "run", timeout)

    errors, observations = runner.collect_backend_observations(
        REPO_ROOT,
        contract_path,
        "python",
        timeout_s=1,
    )

    assert errors == ["python: native command timed out after 1s"]
    assert observations == []


def test_adapter_timeout_defaults_to_120_seconds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = _runner()
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(_contract()), encoding="utf-8")

    def timeout(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] == 120
        raise subprocess.TimeoutExpired(["adapter"], timeout=120)

    monkeypatch.setattr(runner.subprocess, "run", timeout)

    errors, observations = runner.collect_backend_observations(REPO_ROOT, contract_path, "python")

    assert errors == ["python: native command timed out after 120s"]
    assert observations == []


def _mutated_contract(kind: str) -> tuple[dict[str, object], str]:
    contract = copy.deepcopy(_contract())
    scenarios = contract["scenarios"]
    assert isinstance(scenarios, list)
    if kind == "policy":
        scenario = next(item for item in scenarios if item["input"]["policy"]["action"] == "deny")
    else:
        scenario = next(
            item for item in scenarios if "immediate" in item["input"]["workers"]["immediate_output"].values()
        )
    scenario_input = scenario["input"]
    if kind == "member":
        scenario_input["group"]["members"] = ["mutant-worker"]
        scenario_input["visibility"]["readable_members"] = ["mutant-worker"]
        scenario_input["workers"]["accepted_members"] = ["mutant-worker"]
        scenario_input["workers"]["immediate_output"] = {"mutant-worker": "immediate"}
    elif kind == "command":
        scenario_input["command"] = "whoami-mutant"
    elif kind == "policy":
        scenario_input["policy"]["action"] = "allow"
    elif kind == "output":
        scenario_input["workers"]["immediate_output"] = {"w1": "changed-output"}
    return contract, scenario["id"]


def test_fixture_inputs_drive_every_backend_runtime_observations_and_comparator() -> None:
    runner = _runner()
    for backend in sorted(BACKENDS):
        for kind in ("member", "command", "policy", "output"):
            contract, scenario_id = _mutated_contract(kind)
            with tempfile.TemporaryDirectory(prefix=f"fanout-metamorphic-{backend}-{kind}-") as directory:
                contract_path = Path(directory) / "contract.json"
                contract_path.write_text(json.dumps(contract), encoding="utf-8")
                command_errors, observations = runner.collect_backend_observations(REPO_ROOT, contract_path, backend)
            assert command_errors == []
            observation = next(item for item in observations if item["id"] == scenario_id)
            if kind == "member":
                assert observation["delivered_workers"] == ["mutant-worker"]
            elif kind == "command":
                assert observation["command"] == "whoami-mutant"
            elif kind == "policy":
                assert observation["status_code"] == 200 and observation["error"] is None
            else:
                assert observation["output"] == {"w1": "changed-output"}
            assert runner.compare_observations(contract, backend, observations)
