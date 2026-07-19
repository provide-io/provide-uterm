#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate committed golden vectors from spec/behavior.json.

Emits a single JSON file consumed by Python, Go, and C# tests so policy and
hello defaults stay tri-language aligned without placeholder asserts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "spec" / "behavior.json"
OUT_PATH = ROOT / "spec" / "behavior_vectors.json"
GO_COPY = ROOT / "packages" / "provide-uterm-go" / "policy" / "testdata" / "behavior_vectors.json"
GO_VNC_COPY = ROOT / "packages" / "provide-uterm-go" / "vnc" / "testdata" / "behavior_vectors.json"
CS_COPY = (
    ROOT
    / "packages"
    / "provide-uterm-csharp"
    / "tests"
    / "Provide.Uterm.Tests"
    / "testdata"
    / "behavior"
    / "behavior_vectors.json"
)
PY_TEST_COPY = ROOT / "packages" / "provide-uterm" / "tests" / "bridge" / "testdata" / "behavior_vectors.json"


def _role_ok(role: str, minimum: str, roles: dict[str, dict[str, int]]) -> bool:
    return roles[role]["rank"] >= roles[minimum]["rank"]


def build_vectors(spec: dict) -> dict:
    roles = spec["roles"]
    ops = spec["operations"]
    cases: list[dict] = []

    role_names = list(roles.keys())
    for op_name, op in ops.items():
        min_role = op["minimum_role"]
        preconditions = set(op.get("preconditions") or [])
        errors = op.get("error_codes") or {}
        for role in role_names:
            for lease_owned in (True, False):
                for session_active in (True, False):
                    allowed = True
                    error: str | None = None
                    if not _role_ok(role, min_role, roles):
                        allowed = False
                        error = errors.get("forbidden_role") or errors.get("403") or "forbidden: insufficient role"
                    elif "lease_owned" in preconditions and not lease_owned:
                        allowed = False
                        error = errors.get("forbidden_lease") or "forbidden: no active lease"
                    elif "session_active" in preconditions and not session_active:
                        allowed = False
                        error = errors.get("forbidden_session") or "forbidden: session inactive"
                    # Idempotent release with empty preconditions always allows once role ok.
                    cases.append(
                        {
                            "op": op_name,
                            "role": role,
                            "lease_owned": lease_owned,
                            "session_active": session_active,
                            "allowed": allowed,
                            "error": error,
                        }
                    )

    return {
        "version": spec["version"],
        "hello_defaults": spec["hello_defaults"],
        "policy_cases": cases,
    }


def main() -> int:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    vectors = build_vectors(spec)
    text = json.dumps(vectors, indent=2, sort_keys=True) + "\n"
    OUT_PATH.write_text(text, encoding="utf-8")
    for dest in (GO_COPY, GO_VNC_COPY, CS_COPY, PY_TEST_COPY, OUT_PATH):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    print(f"Wrote {OUT_PATH.relative_to(ROOT)} ({len(vectors['policy_cases'])} policy cases)")
    print(f"Copied to {GO_COPY.relative_to(ROOT)}")
    print(f"Copied to {CS_COPY.relative_to(ROOT)}")
    print(f"Copied to {PY_TEST_COPY.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
