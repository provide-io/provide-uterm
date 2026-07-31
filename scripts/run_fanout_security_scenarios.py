#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Execute and compare the shared fan-out security scenarios in every backend."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

BACKENDS = ("python", "go", "csharp", "typescript")
STATUSES = {"execute", "unsupported_fail_closed", "component_execute", "unserved"}
RESULT_FIELDS = {
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
EXPECTED_SURFACES = {
    "python": {"surface": "server", "advertised": True},
    "go": {"surface": "server", "advertised": True},
    "csharp": {"surface": "server", "advertised": True},
    "typescript": {"surface": "component", "advertised": False},
}
INPUT_FIELDS = {"surface", "operation", "actor", "group", "visibility", "policy", "workers", "command"}
REQUIRED_CATEGORY_CARDINALITY = {
    "authentication refusal": 1,
    "viewer refusal": 1,
    "strict dormant-member rejection": 1,
    "permissive dormant-member admission": 1,
    "current authorization revocation": 1,
    "group grant non-bypass": 1,
    "partial member failure": 1,
    "policy deny": 1,
    "policy hold and release": 1,
    "missing authorization dependencies": 1,
    "immediate output capture": 1,
    "store read isolation": 1,
    "store atomic update": 1,
    "total response deadline": 1,
}


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def expected_for(scenario: dict[str, Any], backend: str) -> dict[str, Any]:
    """Resolve a backend's expectation from the common values and overrides."""
    common = _dict(scenario.get("expected"))
    claim = _dict(_dict(scenario.get("backends")).get(backend))
    return {**common, **_dict(claim.get("expected"))}


def applicable_ids(contract: dict[str, Any], backend: str) -> set[str]:
    """Return IDs the backend must execute, excluding explicitly unserved cases."""
    return {
        str(scenario["id"])
        for scenario in contract.get("scenarios", [])
        if _dict(_dict(scenario).get("backends")).get(backend, {}).get("status") != "unserved"
    }


def semantic_status(scenario: dict[str, Any], backend: str) -> str:
    """Derive backend support from scenario semantics rather than its opaque ID."""
    input_data = _dict(scenario.get("input"))
    continuous_output = _dict(input_data.get("workers")).get("continuous_output") is True
    governed = _dict(input_data.get("policy")).get("action") != "allow"
    if backend == "typescript":
        return "unserved" if continuous_output else "component_execute"
    if backend == "python":
        return "unserved" if continuous_output else "execute"
    if backend == "go":
        if continuous_output:
            return "unserved"
        return "unsupported_fail_closed" if governed else "execute"
    return "unsupported_fail_closed" if governed else "execute"


def semantic_categories(scenario: dict[str, Any]) -> set[str]:
    """Classify required security behaviors solely from executable input data."""
    input_data = _dict(scenario.get("input"))
    actor = _dict(input_data.get("actor"))
    group = _dict(input_data.get("group"))
    visibility = _dict(input_data.get("visibility"))
    policy = _dict(input_data.get("policy"))
    workers = _dict(input_data.get("workers"))
    operation = input_data.get("operation")
    roles = set(actor.get("roles", [])) if isinstance(actor.get("roles"), list) else set()
    members = set(group.get("members", [])) if isinstance(group.get("members"), list) else set()
    grants = group.get("grants", []) if isinstance(group.get("grants"), list) else []
    revoked = visibility.get("revoke_before_send", []) if isinstance(visibility.get("revoke_before_send"), list) else []
    accepted = set(workers.get("accepted_members", [])) if isinstance(workers.get("accepted_members"), list) else set()
    immediate = _dict(workers.get("immediate_output"))
    categories: set[str] = set()
    if actor.get("authenticated") is False:
        categories.add("authentication refusal")
    if actor.get("authenticated") is True and "admin" not in roles:
        categories.add("viewer refusal")
    if operation == "create" and group.get("allow_unknown_members") is False:
        categories.add("strict dormant-member rejection")
    if operation == "create" and group.get("allow_unknown_members") is True:
        categories.add("permissive dormant-member admission")
    if revoked and not grants:
        categories.add("current authorization revocation")
    if revoked and grants:
        categories.add("group grant non-bypass")
    if operation == "send" and not revoked and accepted < members:
        categories.add("partial member failure")
    if policy.get("action") == "deny":
        categories.add("policy deny")
    if policy.get("action") == "hold_release":
        categories.add("policy hold and release")
    if input_data.get("omit_authorizers") is True:
        categories.add("missing authorization dependencies")
    if "immediate" in immediate.values():
        categories.add("immediate output capture")
    if operation == "store_read_isolation":
        categories.add("store read isolation")
    if operation == "store_atomic_update":
        categories.add("store atomic update")
    if workers.get("continuous_output") is True and input_data.get("max_response_ms"):
        categories.add("total response deadline")
    return categories


def validate_contract(contract: dict[str, Any]) -> list[str]:
    """Validate the semantic contract and backend support claims."""
    errors: list[str] = []
    if contract.get("schema_version") != 2:
        errors.append("schema_version must be 2")
    if set(contract.get("status_vocabulary", [])) != STATUSES:
        errors.append("status vocabulary mismatch")
    backends = _dict(contract.get("backends"))
    if set(backends) != set(BACKENDS):
        errors.append("backends must be exactly python, go, csharp, and typescript")
    for backend in BACKENDS:
        if _dict(backends.get(backend)) != EXPECTED_SURFACES[backend]:
            if backend == "typescript":
                errors.append("typescript must remain an unserved component and must not advertise a server")
            else:
                errors.append(f"{backend}: server capability metadata mismatch")

    scenarios = contract.get("scenarios")
    if not isinstance(scenarios, list):
        return [*errors, "scenarios must be a list"]
    seen: set[str] = set()
    category_counts = dict.fromkeys(REQUIRED_CATEGORY_CARDINALITY, 0)
    for raw_scenario in scenarios:
        scenario = _dict(raw_scenario)
        for category in semantic_categories(scenario):
            if category in category_counts:
                category_counts[category] += 1
        scenario_id = scenario.get("id")
        if not isinstance(scenario_id, str) or not scenario_id:
            errors.append("scenario id must be a non-empty string")
            continue
        if scenario_id in seen:
            errors.append(f"duplicate scenario id: {scenario_id}")
        seen.add(scenario_id)
        if not isinstance(scenario.get("input"), dict):
            errors.append(f"{scenario_id}: input must be semantic data")
        elif not set(scenario["input"]) >= INPUT_FIELDS:
            errors.append(f"{scenario_id}: input is missing required semantic fields")
        common = _dict(scenario.get("expected"))
        if set(common) != RESULT_FIELDS:
            errors.append(f"{scenario_id}: common expectation must contain every normalized result field")
        claims = _dict(scenario.get("backends"))
        if set(claims) != set(BACKENDS):
            errors.append(f"{scenario_id}: backend claims must be exact")
            continue
        for backend in BACKENDS:
            claim = _dict(claims.get(backend))
            if set(claim) != {"status", "expected"}:
                errors.append(f"{scenario_id}.{backend}: claim must contain status and expected")
                continue
            status = claim.get("status")
            if status not in STATUSES:
                errors.append(f"{scenario_id}.{backend}: invalid status {status!r}")
            if status != semantic_status(scenario, backend):
                errors.append(f"{scenario_id}.{backend}: false capability status {status!r}")
            overrides = _dict(claim.get("expected"))
            if set(overrides) - RESULT_FIELDS or set(expected_for(scenario, backend)) != RESULT_FIELDS:
                errors.append(f"{scenario_id}.{backend}: invalid expected result fields")
    for category, minimum in REQUIRED_CATEGORY_CARDINALITY.items():
        if category_counts[category] < minimum:
            errors.append(f"missing required semantic category: {category}")
    return errors


def compare_observations(contract: dict[str, Any], backend: str, observations: object) -> list[str]:
    """Compare one native adapter's observations with its applicable contract."""
    if not isinstance(observations, list):
        return [f"{backend}: adapter output must be a JSON list"]
    errors: list[str] = []
    expected_ids = applicable_ids(contract, backend)
    by_id: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for raw_observation in observations:
        observation = _dict(raw_observation)
        scenario_id = observation.get("id")
        if not isinstance(scenario_id, str):
            errors.append(f"{backend}: observation without string id")
            continue
        if scenario_id in by_id:
            duplicates.add(scenario_id)
        by_id[scenario_id] = observation
    if duplicates:
        errors.append(f"{backend}: duplicate IDs {sorted(duplicates)}")
    missing = sorted(expected_ids - set(by_id))
    extra = sorted(set(by_id) - expected_ids)
    if missing:
        errors.append(f"{backend}: missing IDs {missing}")
    if extra:
        errors.append(f"{backend}: extra IDs {extra}")

    scenarios = {scenario["id"]: scenario for scenario in contract["scenarios"]}
    for scenario_id in sorted(expected_ids & set(by_id)):
        observation = by_id[scenario_id]
        if observation.get("skipped") is True:
            errors.append(f"{backend}.{scenario_id}: skip is not executable evidence")
        scenario = scenarios[scenario_id]
        claim = scenario["backends"][backend]
        if observation.get("status") != claim["status"]:
            errors.append(
                f"{backend}.{scenario_id}: status mismatch: {observation.get('status')!r} != {claim['status']!r}"
            )
        expected = expected_for(scenario, backend)
        for field, value in expected.items():
            if observation.get(field) != value:
                errors.append(
                    f"{backend}.{scenario_id}.{field}: result mismatch: {observation.get(field)!r} != {value!r}"
                )
    return errors


def command_errors(backend: str, result: subprocess.CompletedProcess[str]) -> list[str]:
    """Describe a failed native command without mistaking it for a skip."""
    if result.returncode == 0:
        return []
    detail = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    return [f"{backend}: native command failed ({result.returncode}): {detail}"]


def _command(root: Path, backend: str) -> tuple[list[str], Path]:
    if backend == "python":
        return [
            "uv",
            "run",
            "pytest",
            "-q",
            "--no-cov",
            "packages/provide-uterm-server/tests/bridge/test_fanout_security_scenarios.py",
        ], root
    if backend == "go":
        return [
            "go",
            "test",
            "./server",
            "-run",
            "^TestFanoutSecurityScenarios$",
            "-count=1",
        ], root / "packages/provide-uterm-go"
    if backend == "csharp":
        return [
            "dotnet",
            "test",
            "tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj",
            "--no-restore",
            "--filter",
            "FullyQualifiedName~FanoutSecurityScenarioTests",
            "--",
            "xUnit.ParallelizeAssembly=false",
            "xUnit.MaxParallelThreads=1",
        ], root / "packages/provide-uterm-csharp"
    return ["npx", "vitest", "run", "src/fanout/security-scenarios.test.ts"], root / "packages/provide-uterm-ts"


def collect_backend_observations(
    root: Path, contract_path: Path, backend: str
) -> tuple[list[str], list[dict[str, Any]]]:
    """Run one native adapter and return only observations produced by it."""
    with tempfile.TemporaryDirectory(prefix=f"uterm-fanout-{backend}-") as directory:
        output_path = Path(directory) / "observations.json"
        environment = {
            **os.environ,
            "FANOUT_SECURITY_SCENARIO_CONTRACT": str(contract_path),
            "FANOUT_SECURITY_SCENARIO_OUTPUT": str(output_path),
        }
        if backend == "go":
            environment["GOWORK"] = "off"
        command, cwd = _command(root, backend)
        result = subprocess.run(command, cwd=cwd, env=environment, check=False, capture_output=True, text=True)
        errors = command_errors(backend, result)
        if errors:
            return errors, []
        if not output_path.is_file():
            return [f"{backend}: native command produced no observation file"], []
        try:
            observations = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [f"{backend}: invalid observation JSON: {exc}"], []
        if not isinstance(observations, list) or not all(isinstance(item, dict) for item in observations):
            return [f"{backend}: adapter output must be a JSON list of objects"], []
        return [], observations


def run_backend(root: Path, contract_path: Path, contract: dict[str, Any], backend: str) -> list[str]:
    """Run one native adapter and compare its runtime observations."""
    errors, observations = collect_backend_observations(root, contract_path, backend)
    return errors or compare_observations(contract, backend, observations)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--contract", type=Path, default=Path("spec/fanout_security_scenarios.json"))
    parser.add_argument("--backend", action="append", choices=BACKENDS)
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    contract_path = args.contract if args.contract.is_absolute() else root / args.contract
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"fan-out security scenario contract failed: {exc}")
        return 1
    errors = validate_contract(_dict(contract))
    if not args.validate_only and not errors:
        for backend in args.backend or BACKENDS:
            errors.extend(run_backend(root, contract_path, contract, backend))
    if errors:
        print("fan-out security scenarios failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"fan-out security scenarios passed for {', '.join(args.backend or BACKENDS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
