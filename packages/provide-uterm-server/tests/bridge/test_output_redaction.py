#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from provide.uterm.control_channel import ControlChannelDecoder
from provide.uterm.server.bridge.hub import PolicyContext, TermHub
from provide.uterm.server.bridge.hub.ext import OutputPolicyGate, RedactionRule


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


@pytest.mark.asyncio
async def test_broadcast_builds_policy_context_once_per_frame() -> None:
    """SRV-bcast: a single frame to N same-role viewers builds the policy context once.

    The pre-fix code called ``prepare_policy_context`` (which re-acquires the
    hub lock) and ``get_redaction_rules`` once per browser per frame, so N
    viewers meant N policy builds and N lock acquisitions per terminal frame.
    The context/rules are now built once per distinct viewer role per frame.
    """
    rules = [RedactionRule(pattern=r"sk_live_[0-9a-zA-Z]+", replacement="[STRIPE_SECRET]")]
    hub = TermHub(output_policy_gate=MockOutputPolicy(rules))
    worker_id = "w1"
    await hub.register_worker(worker_id, AsyncMock())

    viewers = [AsyncMock() for _ in range(5)]
    for ws in viewers:
        await hub.register_browser(worker_id, ws, "viewer")

    calls = 0
    orig = hub.prepare_policy_context

    async def spy(*args: object, **kwargs: object) -> PolicyContext:
        nonlocal calls
        calls += 1
        return await orig(*args, **kwargs)  # type: ignore[arg-type]

    hub.prepare_policy_context = spy  # type: ignore[method-assign]

    await hub.broadcast(worker_id, {"type": "term", "data": "key sk_live_abc123 here"})

    assert calls == 1, f"expected one policy-context build for 5 same-role viewers, got {calls}"
    # Redaction still applied to every viewer.
    decoder = ControlChannelDecoder()
    for ws in viewers:
        redacted = False
        for call in ws.send_text.call_args_list:
            for event in decoder.feed(call[0][0]):
                if event.kind == "data" and "[STRIPE_SECRET]" in event.data:
                    redacted = True
        assert redacted, "viewer did not receive redacted data"


@pytest.mark.asyncio
async def test_broadcast_builds_policy_context_per_distinct_role() -> None:
    """Different viewer roles each get their own policy context within one frame."""
    rules = [RedactionRule(pattern=r"sk_live_[0-9a-zA-Z]+", replacement="[STRIPE_SECRET]")]
    hub = TermHub(output_policy_gate=MockOutputPolicy(rules))
    worker_id = "w1"
    await hub.register_worker(worker_id, AsyncMock())
    await hub.register_browser(worker_id, AsyncMock(), "viewer")
    await hub.register_browser(worker_id, AsyncMock(), "operator")

    calls = 0
    orig = hub.prepare_policy_context

    async def spy(*args: object, **kwargs: object) -> PolicyContext:
        nonlocal calls
        calls += 1
        return await orig(*args, **kwargs)  # type: ignore[arg-type]

    hub.prepare_policy_context = spy  # type: ignore[method-assign]

    await hub.broadcast(worker_id, {"type": "term", "data": "x"})

    assert calls == 2, f"expected one build per distinct role (2), got {calls}"


@pytest.mark.asyncio
async def test_hub_output_gate_empty_rules_sends_unredacted_default() -> None:
    """A configured output gate that returns *no* rules falls through to the
    default (unredacted) payload.

    Covers the empty-rules ``else`` branch in ``broadcast`` (the gate is active
    so the redaction path runs, but ``get_redaction_rules`` returns ``[]`` so
    the cached default frame is used)."""
    gate = MockOutputPolicy([])  # active gate, but no redaction rules
    hub = TermHub(output_policy_gate=gate)

    ws = AsyncMock()
    worker_id = "w1"
    await hub.register_worker(worker_id, AsyncMock())
    await hub.register_browser(worker_id, ws, "viewer")

    raw_data = "plain terminal output with no secrets"
    await hub.broadcast(worker_id, {"type": "term", "data": raw_data})

    found_plain = False
    decoder = ControlChannelDecoder()
    for call in ws.send_text.call_args_list:
        payload = call[0][0]
        for event in decoder.feed(payload):
            if event.kind == "data" and raw_data in event.data:
                found_plain = True
    assert found_plain, "default (unredacted) terminal data was not broadcast"
