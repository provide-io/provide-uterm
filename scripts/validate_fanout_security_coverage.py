#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Validate fan-out security capability claims and their executable evidence."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

EXPECTED_BACKENDS = {"python", "go", "csharp", "typescript"}
EXPECTED_BEHAVIORS = {
    "unknown_members_default_reject",
    "unknown_members_explicit_permissive",
    "send_rechecks_current_authorization",
    "group_grant_does_not_bypass_session_access",
    "partial_failures",
    "policy_deny",
    "policy_hold_release",
}
COVERAGE_STATUSES = {"covered", "component_covered", "unsupported"}
DELIVERY_CAPABILITIES = {"served", "unserved"}
POLICY_CAPABILITIES = {"implemented", "implemented_component_only", "unsupported_fail_closed"}
POLICY_BEHAVIORS = {"policy_deny", "policy_hold_release"}
BACKEND_TEST_ROOTS = {
    "python": "packages/provide-uterm-server/tests/",
    "go": "packages/provide-uterm-go/",
    "csharp": "packages/provide-uterm-csharp/tests/",
    "typescript": "packages/provide-uterm-ts/src/",
}
EXPECTED_CAPABILITIES = {
    "python": {"delivery": "served", "policy": "implemented"},
    "go": {"delivery": "served", "policy": "unsupported_fail_closed"},
    "csharp": {"delivery": "served", "policy": "unsupported_fail_closed"},
    "typescript": {"delivery": "unserved", "policy": "implemented_component_only"},
}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _is_test_declaration(backend: str, test_id: str, content: str) -> bool:
    escaped = re.escape(test_id)
    patterns = {
        "python": rf"(?m)^\s*(?:async\s+)?def\s+{escaped}\s*\(",
        "go": rf"(?m)^func\s+{escaped}\s*\(",
        "csharp": rf"(?m)^\s*public\s+(?:async\s+)?(?:void|Task)\s+{escaped}\s*\(",
        "typescript": rf"\b(?:it|test)\(\s*([\"'`]){escaped}\1\s*,",
    }
    return re.search(patterns[backend], content) is not None


def _validate_evidence(root: Path, backend: str, label: str, evidence: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(evidence, list) or not evidence:
        return [f"{backend}.{label}: executable evidence must be a non-empty list"]
    for index, raw_item in enumerate(evidence):
        item = _as_dict(raw_item)
        path_text = item.get("file")
        test_id = item.get("test")
        subject = f"{backend}.{label}.evidence[{index}]"
        if not isinstance(path_text, str) or not path_text.startswith(BACKEND_TEST_ROOTS[backend]):
            errors.append(f"{subject}: file must be inside {BACKEND_TEST_ROOTS[backend]}")
            continue
        if not isinstance(test_id, str) or not test_id.strip():
            errors.append(f"{subject}: test must be a non-empty identifier")
            continue
        candidate = (root / path_text).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            errors.append(f"{subject}: file escapes repository root")
            continue
        if not candidate.is_file():
            errors.append(f"{subject}: missing test file {path_text}")
            continue
        content = candidate.read_text(encoding="utf-8")
        if not _is_test_declaration(backend, test_id, content):
            errors.append(f"{subject}: test declaration {test_id!r} not found in {path_text}")
    return errors


def validate_manifest(root: Path, manifest: dict[str, Any]) -> list[str]:
    """Return all structural and evidence errors in a decoded manifest."""
    errors: list[str] = []
    if manifest.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if set(manifest.get("status_vocabulary", [])) != COVERAGE_STATUSES:
        errors.append("status_vocabulary does not match the validator vocabulary")
    vocab = _as_dict(manifest.get("capability_vocabulary"))
    if set(vocab.get("delivery", [])) != DELIVERY_CAPABILITIES:
        errors.append("delivery capability vocabulary does not match the validator vocabulary")
    if set(vocab.get("policy", [])) != POLICY_CAPABILITIES:
        errors.append("policy capability vocabulary does not match the validator vocabulary")
    if set(manifest.get("required_behaviors", [])) != EXPECTED_BEHAVIORS:
        errors.append("required_behaviors must contain the complete fan-out security behavior set")

    backends = _as_dict(manifest.get("backends"))
    if set(backends) != EXPECTED_BACKENDS:
        errors.append("backends must contain exactly python, go, csharp, and typescript")

    for backend in sorted(EXPECTED_BACKENDS & set(backends)):
        entry = _as_dict(backends[backend])
        capabilities = _as_dict(entry.get("capabilities"))
        delivery = capabilities.get("delivery")
        policy = capabilities.get("policy")
        if delivery not in DELIVERY_CAPABILITIES:
            errors.append(f"{backend}: invalid delivery capability {delivery!r}")
        if policy not in POLICY_CAPABILITIES:
            errors.append(f"{backend}: invalid policy capability {policy!r}")
        if capabilities != EXPECTED_CAPABILITIES[backend]:
            errors.append(f"{backend}: capabilities do not match repository support matrix")
        if delivery == "served":
            errors.extend(_validate_evidence(root, backend, "served_evidence", entry.get("served_evidence")))
        elif "served_evidence" in entry:
            errors.append(f"{backend}: unserved backend must not claim served_evidence")

        if delivery == "served" and policy == "implemented_component_only":
            errors.append(f"{backend}: served delivery cannot claim component-only policy")
        if delivery == "unserved" and policy != "implemented_component_only":
            errors.append(f"{backend}: unserved delivery must use implemented_component_only policy")

        coverage = _as_dict(entry.get("coverage"))
        if set(coverage) != EXPECTED_BEHAVIORS:
            errors.append(f"{backend}: coverage must contain every required behavior exactly once")
        for behavior in sorted(EXPECTED_BEHAVIORS & set(coverage)):
            claim = _as_dict(coverage[behavior])
            status = claim.get("status")
            if status not in COVERAGE_STATUSES:
                errors.append(f"{backend}.{behavior}: invalid status {status!r}")
            expected_status = "covered" if delivery == "served" else "component_covered"
            if behavior in POLICY_BEHAVIORS and policy == "unsupported_fail_closed":
                expected_status = "unsupported"
            if status != expected_status:
                errors.append(
                    f"{backend}.{behavior}: status {status!r} conflicts with capabilities; expected {expected_status!r}"
                )
            errors.extend(_validate_evidence(root, backend, behavior, claim.get("evidence")))
    return errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path, default=Path("spec/fanout_security_coverage.json"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    manifest_path = args.manifest if args.manifest.is_absolute() else root / args.manifest
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"fan-out security coverage validation failed: {exc}")
        return 1
    errors = validate_manifest(root, _as_dict(manifest))
    if errors:
        print("fan-out security coverage validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("fan-out security coverage manifest is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
