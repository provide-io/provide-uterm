#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Execute the shared public-route session-lifecycle security scenarios."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

BACKENDS = ("python", "go", "csharp", "typescript", "cloudflare")
EXECUTABLE_BACKENDS = ("python", "go", "csharp", "cloudflare")
STATUSES = {"served", "unsupported", "unserved"}
RESULT_FIELDS = {
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
}
OBSERVATION_FIELDS = {"id", "status", *RESULT_FIELDS}
REQUIRED_CATEGORY_CARDINALITY = {
    "message fragmentation": 3,
    "browser quota accounting": 1,
    "configured governance": 3,
    "resume ownership and replay": 2,
    "non-owner hijack-step refusal": 1,
}
EXPECTED_SURFACES = {
    "python": {"surface": "server", "advertised": True},
    "go": {"surface": "server", "advertised": True},
    "csharp": {"surface": "server", "advertised": True},
    "typescript": {"surface": "component", "advertised": False},
    "cloudflare": {"surface": "server", "advertised": True},
}


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def expected_for(contract: dict[str, Any], scenario: dict[str, Any], backend: str) -> dict[str, Any]:
    """Resolve a backend expectation from common values plus its overrides."""
    claim = _dict(_dict(scenario.get("backends")).get(backend))
    common = {} if claim.get("status") == "unserved" else _dict(scenario.get("expected"))
    return {**_dict(contract.get("result_defaults")), **common, **_dict(claim.get("expected"))}


def semantic_category(scenario: dict[str, Any]) -> str:
    """Classify behavior from executable input rather than the scenario ID."""
    operation = _dict(scenario.get("input")).get("operation")
    categories = {
        "fragment_message": "message fragmentation",
        "browser_quota": "browser quota accounting",
        "governed_input": "configured governance",
        "resume_ownership": "resume ownership and replay",
        "non_owner_hijack_step": "non-owner hijack-step refusal",
    }
    return categories.get(operation, "unknown")


def semantic_status(scenario: dict[str, Any], backend: str) -> str:
    """Derive support from the served surface and scenario semantics."""
    input_data = _dict(scenario.get("input"))
    operation = input_data.get("operation")
    if backend == "typescript":
        return "unserved"
    if backend == "cloudflare":
        if operation == "fragment_message" and input_data.get("transport") == "tunnel":
            return "unserved"
        if operation in {"browser_quota", "governed_input"}:
            return "unsupported"
        return "served"
    if operation == "governed_input" and backend in {"go", "csharp"}:
        return "unsupported"
    return "served"


def applicable_ids(contract: dict[str, Any], backend: str) -> set[str]:
    """Return scenario IDs for which the backend owes executable evidence."""
    return {
        str(scenario["id"])
        for scenario in contract.get("scenarios", [])
        if _dict(_dict(scenario).get("backends")).get(backend, {}).get("status") != "unserved"
    }


def validate_contract(contract: dict[str, Any]) -> list[str]:
    """Validate category coverage, exact backend cells, and truthful statuses."""
    errors: list[str] = []
    if contract.get("schema_version") != 2:
        errors.append("schema_version must be 2")
    if set(contract.get("status_vocabulary", [])) != STATUSES:
        errors.append("status vocabulary mismatch")
    backends = _dict(contract.get("backends"))
    if set(backends) != set(BACKENDS):
        errors.append("backends must be exactly python, go, csharp, typescript, and cloudflare")
    for backend in BACKENDS:
        if _dict(backends.get(backend)) != EXPECTED_SURFACES[backend]:
            errors.append(f"{backend}: capability metadata mismatch")

    if set(_dict(contract.get("result_defaults"))) != RESULT_FIELDS:
        errors.append("result_defaults must contain every normalized result field")
    raw_scenarios = contract.get("scenarios")
    if not isinstance(raw_scenarios, list):
        return [*errors, "scenarios must be a list"]
    seen: set[str] = set()
    category_counts = dict.fromkeys(REQUIRED_CATEGORY_CARDINALITY, 0)
    for raw_scenario in raw_scenarios:
        scenario = _dict(raw_scenario)
        category = semantic_category(scenario)
        if category in REQUIRED_CATEGORY_CARDINALITY:
            category_counts[category] += 1
        else:
            errors.append(f"unknown semantic category for scenario {scenario.get('id')!r}")
        scenario_id = scenario.get("id")
        if not isinstance(scenario_id, str) or not scenario_id:
            errors.append("scenario id must be a non-empty string")
            continue
        if scenario_id in seen:
            errors.append(f"duplicate scenario id: {scenario_id}")
        seen.add(scenario_id)
        if not isinstance(scenario.get("input"), dict):
            errors.append(f"{scenario_id}: input must be semantic data")
        if set(_dict(scenario.get("expected"))) - RESULT_FIELDS:
            errors.append(f"{scenario_id}: common expectation has unknown normalized result fields")
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
            if status != "unserved" and backend not in EXECUTABLE_BACKENDS:
                errors.append(f"{scenario_id}.{backend}: claimed cell has no executable native adapter")
            overrides = _dict(claim.get("expected"))
            if set(overrides) - RESULT_FIELDS or set(expected_for(contract, scenario, backend)) != RESULT_FIELDS:
                errors.append(f"{scenario_id}.{backend}: invalid expected result fields")
    for category, minimum in REQUIRED_CATEGORY_CARDINALITY.items():
        if category_counts[category] < minimum:
            errors.append(
                f"missing required semantic category: {category}: expected at least {minimum}, got {category_counts[category]}"
            )
    return errors


def compare_observations(contract: dict[str, Any], backend: str, observations: object) -> list[str]:
    """Compare one native adapter's complete observations with the contract."""
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
        if set(observation) != OBSERVATION_FIELDS:
            errors.append(
                f"{backend}.{scenario_id}: observation fields mismatch: {sorted(set(observation) ^ OBSERVATION_FIELDS)}"
            )
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
        for field, value in expected_for(contract, scenario, backend).items():
            if observation.get(field) != value:
                errors.append(
                    f"{backend}.{scenario_id}.{field}: result mismatch: {observation.get(field)!r} != {value!r}"
                )
    return errors


def _command(root: Path, backend: str) -> tuple[list[str], Path]:
    if backend == "python":
        return [
            "uv",
            "run",
            "pytest",
            "-q",
            "--no-cov",
            "packages/provide-uterm-server/tests/bridge/test_session_lifecycle_security_scenarios.py",
        ], root
    if backend == "go":
        return ["go", "test", "./server", "-run", "^TestSessionLifecycleSecurityScenarios$", "-count=1"], (
            root / "packages/provide-uterm-go"
        )
    if backend == "csharp":
        return [
            "dotnet",
            "test",
            "tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj",
            "--no-restore",
            "--filter",
            "FullyQualifiedName~SessionLifecycleSecurityScenarioTests",
            "--",
            "xUnit.ParallelizeAssembly=false",
            "xUnit.MaxParallelThreads=1",
        ], root / "packages/provide-uterm-csharp"
    if backend == "cloudflare":
        return [
            "uv",
            "run",
            "pytest",
            "-q",
            "--no-cov",
            "packages/provide-uterm-cloudflare/tests/test_session_lifecycle_security_scenarios.py",
        ], root
    raise ValueError(f"{backend} has no served adapter")


def collect_backend_observations(
    root: Path,
    contract_path: Path,
    backend: str,
    *,
    timeout_s: float = 120,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Run a native adapter and require its machine-readable output file."""
    with tempfile.TemporaryDirectory(prefix=f"uterm-lifecycle-{backend}-") as directory:
        output_path = Path(directory) / "observations.json"
        environment = {
            **os.environ,
            "SESSION_LIFECYCLE_SCENARIO_CONTRACT": str(contract_path),
            "SESSION_LIFECYCLE_SCENARIO_OUTPUT": str(output_path),
        }
        if backend == "go":
            environment["GOWORK"] = "off"
        command, cwd = _command(root, backend)
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return [f"{backend}: native command timed out after {timeout_s:g}s"], []
        if result.returncode != 0:
            detail = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
            return [f"{backend}: native command failed ({result.returncode}): {detail}"], []
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
    """Run and compare one backend, without launching an unserved component."""
    if not applicable_ids(contract, backend):
        return []
    # The Cloudflare adapter performs a real Python-Worker bundle and boots
    # workerd through pywrangler before exercising the public routes.  Keep its
    # budget explicit instead of weakening the timeout for every native port.
    timeout_s = 300 if backend == "cloudflare" else 120
    errors, observations = collect_backend_observations(root, contract_path, backend, timeout_s=timeout_s)
    return errors or compare_observations(contract, backend, observations)


def _parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root)
    parser.add_argument("--contract", type=Path, default=root / "spec/session_lifecycle_security_scenarios.json")
    parser.add_argument("--backend", action="append", choices=EXECUTABLE_BACKENDS)
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    contract_path = args.contract if args.contract.is_absolute() else root / args.contract
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"session-lifecycle security scenario contract failed: {exc}")
        return 1
    errors = validate_contract(_dict(contract))
    selected = args.backend or list(EXECUTABLE_BACKENDS)
    if not args.validate_only and not errors:
        for backend in selected:
            errors.extend(run_backend(root, contract_path, contract, backend))
    if errors:
        print("session-lifecycle security scenarios failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    if args.validate_only:
        print("session-lifecycle contract validation passed")
        return 0
    print(f"session-lifecycle security scenarios passed for {', '.join(selected)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
