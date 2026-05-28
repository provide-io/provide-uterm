#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import pytest

from provide.uterm.server.bridge.hub import NoOpPolicyGate, PolicyContext, TermHub


@pytest.mark.asyncio
async def test_hub_initializes_with_default_ext() -> None:
    hub = TermHub()
    assert isinstance(hub._policy_gate, NoOpPolicyGate)


@pytest.mark.asyncio
async def test_hub_accepts_custom_ext() -> None:
    class CustomPolicyGate:
        async def intercept_input(self, _data: str, _context: PolicyContext) -> bool:
            return False

    policy_gate = CustomPolicyGate()
    hub = TermHub(policy_gate=policy_gate)

    assert hub._policy_gate is policy_gate

    # Basic smoke test for protocols
    assert await hub._policy_gate.intercept_input("hello", PolicyContext(worker_id="test")) is False


@pytest.mark.asyncio
async def test_hub_browser_count_total() -> None:
    hub = TermHub()

    # Mock some browser counts
    class MockState:
        def __init__(self, count: int):
            self.browsers = dict.fromkeys(range(count), "viewer")

    hub._workers = {
        "w1": MockState(2),
        "w2": MockState(3),
    }
    assert await hub.browser_count_total() == 5
