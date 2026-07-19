#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Parametrized + property tests for bridge.policy against committed goldens."""

from __future__ import annotations

import json
from pathlib import Path

import hypothesis.strategies as st
import pytest
from hypothesis import given, settings

from provide.uterm.bridge.policy import can_inject, can_perform
from provide.uterm.bridge.schemas import HelloFrame
from provide.uterm.server.bridge.frames import make_hello_frame


def _load_vectors() -> dict:
    here = Path(__file__).resolve()
    candidates = [
        here.parents[4] / "spec" / "behavior_vectors.json",  # repo root
        here.parents[5] / "spec" / "behavior_vectors.json",  # mutants/<root>
        here.parent / "testdata" / "behavior_vectors.json",
    ]
    for path in candidates:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError("behavior_vectors.json not found in " + ", ".join(str(c) for c in candidates))


VECTORS = _load_vectors()
POLICY_CASES = VECTORS["policy_cases"]
HELLO_DEFAULTS = VECTORS["hello_defaults"]


@pytest.mark.parametrize(
    "case",
    POLICY_CASES,
    ids=[f"{c['op']}-{c['role']}-lease={c['lease_owned']}-sess={c['session_active']}" for c in POLICY_CASES],
)
def test_policy_matches_behavior_vectors(case: dict) -> None:
    err = can_perform(
        case["op"],
        role=case["role"],
        lease_owned=case["lease_owned"],
        session_active=case["session_active"],
    )
    if case["allowed"]:
        assert err is None, case
    else:
        assert err == case["error"], case


def test_can_inject_matches_input_inject_op() -> None:
    assert can_inject("s1", "lease-1", "operator") is None
    assert can_inject("s1", "", "operator") == "forbidden: no active lease"
    assert can_inject("s1", "lease-1", "viewer") == "forbidden: insufficient role"
    # session_id must not affect the decision (del, not dead store)
    assert can_inject("", "lease-1", "operator") is None
    assert can_inject("other", "lease-1", "operator") is None


def test_default_session_active_is_true() -> None:
    """Omitting session_active must keep hijack_step allowed when lease held."""
    assert can_perform("hijack_step", role="operator", lease_owned=True) is None
    assert (
        can_perform("hijack_step", role="operator", lease_owned=True, session_active=False)
        == "forbidden: session inactive"
    )


def test_unknown_role_denied() -> None:
    from provide.uterm.bridge.policy import role_rank

    assert role_rank("guest") == -1
    assert role_rank("") == -1
    assert can_perform("input_inject", role="guest", lease_owned=True) == "forbidden: insufficient role"
    assert can_perform("hijack_acquire", role="not-a-role", lease_owned=True) == "forbidden: insufficient role"


def test_unknown_operation() -> None:
    err = can_perform("nope", role="admin", lease_owned=True)
    assert err == "forbidden: unknown operation nope"


def test_python_hello_defaults_from_contract() -> None:
    expected = HELLO_DEFAULTS["python_fastapi"]
    frame = make_hello_frame()
    assert frame["mcp_supported"] is expected["mcp_supported"]
    assert frame["vnc_supported"] is expected["vnc_supported"]
    parsed = HelloFrame(type="hello", **{k: frame[k] for k in ("mcp_supported", "vnc_supported")})
    assert parsed.mcp_supported is expected["mcp_supported"]
    assert parsed.vnc_supported is expected["vnc_supported"]


@given(
    role=st.sampled_from(["viewer", "operator", "admin", "guest", ""]),
    lease=st.booleans(),
    session=st.booleans(),
    op=st.sampled_from(["input_inject", "hijack_step", "hijack_release", "hijack_acquire", "nope"]),
)
@settings(max_examples=80)
def test_policy_property_no_crash(role: str, lease: bool, session: bool, op: str) -> None:
    err = can_perform(op, role=role, lease_owned=lease, session_active=session)
    if err is not None:
        assert err.startswith("forbidden:")
