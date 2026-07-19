#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Parity smoke: behavior vectors drive the shipped Python policy evaluator."""

from __future__ import annotations

import json
from pathlib import Path

from provide.uterm.bridge.policy import can_perform


def _load_vectors() -> dict:
    here = Path(__file__).resolve()
    for path in (
        here.parents[4] / "spec" / "behavior_vectors.json",
        here.parents[5] / "spec" / "behavior_vectors.json",
        here.parent / "testdata" / "behavior_vectors.json",
    ):
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError("behavior_vectors.json not found")


VECTORS = _load_vectors()


def test_parity_all_vector_cases_via_can_perform() -> None:
    failures: list[str] = []
    for case in VECTORS["policy_cases"]:
        err = can_perform(
            case["op"],
            role=case["role"],
            lease_owned=case["lease_owned"],
            session_active=case["session_active"],
        )
        allowed = err is None
        if allowed != case["allowed"] or (not allowed and err != case["error"]):
            failures.append(f"{case} -> {err!r}")
    assert not failures, "\n".join(failures[:20])
