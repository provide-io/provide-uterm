#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import pytest
from httpx import Response

from provide.uterm.server.bridge.hub.ext import (
    NoOpPolicyGate,
    PolicyContext,
    PolicyDecision,
    WebhookPolicyGate,
)
from tests.helpers import http_mock as respx


def test_policy_decision_model() -> None:
    """Test PolicyDecision model validation."""
    # Valid allow
    d = PolicyDecision(action="allow")
    assert d.action == "allow"
    assert d.timeout_s == 60

    # Valid hold
    d = PolicyDecision(action="hold", request_id="req123", timeout_s=30, reason="Approval needed")
    assert d.action == "hold"
    assert d.request_id == "req123"
    assert d.timeout_s == 30
    assert d.reason == "Approval needed"

    # Invalid action
    with pytest.raises(ValueError):
        PolicyDecision(action="invalid")


@pytest.mark.asyncio
async def test_noop_policy_gate_returns_decision() -> None:
    gate = NoOpPolicyGate()
    ctx = PolicyContext(worker_id="w1")
    result = await gate.intercept_input("hello", ctx)

    assert isinstance(result, PolicyDecision)
    assert result.action == "allow"


@pytest.mark.asyncio
@respx.mock
async def test_webhook_policy_gate_returns_decision() -> None:
    url = "https://fleet.example.com/policy"
    gate = WebhookPolicyGate(url=url)

    # Mock allow response
    respx.post(url).mock(return_value=Response(200, json={"allow": True}))
    ctx = PolicyContext(worker_id="w1")
    result = await gate.intercept_input("ls", ctx)
    assert result.action == "allow"

    # Mock deny response
    respx.post(url).mock(return_value=Response(200, json={"allow": False}))
    result = await gate.intercept_input("rm", ctx)
    assert result.action == "deny"

    # Mock hold response
    respx.post(url).mock(
        return_value=Response(
            200, json={"action": "hold", "request_id": "r1", "timeout_s": 120, "reason": "Wait for admin"}
        )
    )
    result = await gate.intercept_input("sudo", ctx)
    assert result.action == "hold"
    assert result.request_id == "r1"
    assert result.timeout_s == 120
    assert result.reason == "Wait for admin"
