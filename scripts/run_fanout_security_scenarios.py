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
    surface = input_data.get("surface")
    continuous_output = _dict(input_data.get("workers")).get("continuous_output") is True
    governed = _dict(input_data.get("policy")).get("action") != "allow"
    if backend == "typescript":
        return "unserved" if surface == "store" or continuous_output else "component_execute"
    if backend == "python":
        return "unserved" if surface == "store" or continuous_output else "execute"
    if backend == "go":
        if continuous_output:
            return "unserved"
        return "unsupported_fail_closed" if governed else "execute"
    return "unsupported_fail_closed" if governed else "execute"


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
    for raw_scenario in scenarios:
        scenario = _dict(raw_scenario)
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
