#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from provide.terminal.bridge.hub import PolicyContext, TermHub
from provide.terminal.bridge.hub.ext import OutputPolicyGate, RedactionRule
from provide.terminal.control_channel import ControlChannelDecoder


class MockOutputPolicy(OutputPolicyGate):
    def __init__(self, rules: list[RedactionRule]):
        self.rules = rules

    async def get_redaction_rules(self, _context: PolicyContext) -> list[RedactionRule]:
        return self.rules


@pytest.mark.asyncio
async def test_hub_output_redaction_broadcast() -> None:
    # 1. Setup Hub with a redactor that masks Stripe keys
    rules = [RedactionRule(pattern=r"sk_live_[0-9a-zA-Z]+", replacement="[STRIPE_SECRET]")]
    gate = MockOutputPolicy(rules)
    hub = TermHub(output_policy_gate=gate)

    # 2. Register a browser
    ws = AsyncMock()
    worker_id = "w1"
    await hub.register_worker(worker_id, AsyncMock())
    await hub.register_browser(worker_id, ws, "viewer")

    # 3. Broadcast sensitive terminal data
    raw_data = "Your key is sk_live_123456789abc and it is active."
    await hub.broadcast(worker_id, {"type": "term", "data": raw_data})

    # 4. Verify the WebSocket received redacted data
    # We need to find the call with the term data
    found_redacted = False
    decoder = ControlChannelDecoder()
    for call in ws.send_text.call_args_list:
        payload = call[0][0]
        events = decoder.feed(payload)
        for event in events:
            if event.kind == "data" and "[STRIPE_SECRET]" in event.data:
                found_redacted = True
                assert "sk_live_" not in event.data

    assert found_redacted, "Terminal data was not redacted in broadcast"


@pytest.mark.asyncio
async def test_hub_output_redaction_noop_default() -> None:
    # Default hub should not redact
    hub = TermHub()
    ws = AsyncMock()
    worker_id = "w1"
    await hub.register_worker(worker_id, AsyncMock())
    await hub.register_browser(worker_id, ws, "viewer")

    raw_data = "secret_123"
    await hub.broadcast(worker_id, {"type": "term", "data": raw_data})

    # Check that data reached ws unredacted
    decoder = ControlChannelDecoder()
    received_data = ""
    for call in ws.send_text.call_args_list:
        events = decoder.feed(call[0][0])
        for event in events:
            if event.kind == "data":
                received_data += event.data

    assert "secret_123" in received_data
