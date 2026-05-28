#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.control_channel import ControlChannelDecoder, DataChunk
from provide.uterm.server.bridge.hub import PolicyContext, PolicyDecision, TermHub
from provide.uterm.server.bridge.routes.browser_handlers import _handle_input


@pytest.mark.asyncio
async def test_policy_gate_allow_all() -> None:
    """Default NoOpPolicyGate allows all input."""
    hub = TermHub()
    ws = AsyncMock()
    worker_ws = AsyncMock()

    worker_id = "w1"
    await hub.register_worker(worker_id, worker_ws)
    await hub.register_browser(worker_id, ws, "admin")

    # Acquire hijack so we can send input
    await hub.try_acquire_ws_hijack(worker_id, ws)

    msg = {"type": "input", "data": "hello"}
    await _handle_input(hub, ws, worker_id, msg)

    # Should be sent to worker
    worker_ws.send_text.assert_called()
    payload = worker_ws.send_text.call_args[0][0]

    decoder = ControlChannelDecoder()
    events = decoder.feed(payload)
    chunks = [e.data for e in events if isinstance(e, DataChunk)]
    assert chunks == ["hello"]


@pytest.mark.asyncio
async def test_policy_gate_deny_all() -> None:
    """Custom DenyPolicyGate blocks specific input."""

    class DenyPolicyGate:
        async def intercept_input(self, _data: str, _context: PolicyContext) -> PolicyDecision:
            return PolicyDecision(action="deny")

    hub = TermHub(policy_gate=DenyPolicyGate())
    ws = AsyncMock()
    worker_ws = AsyncMock()

    worker_id = "w1"
    await hub.register_worker(worker_id, worker_ws)
    await hub.register_browser(worker_id, ws, "admin")
    await hub.try_acquire_ws_hijack(worker_id, ws)

    msg = {"type": "input", "data": "hello"}
    await _handle_input(hub, ws, worker_id, msg)

    # Should NOT be sent to worker
    worker_ws.send_text.assert_not_called()


@pytest.mark.asyncio
async def test_policy_context_fields() -> None:
    """Context contains correct worker_id, client_id (principal), and role."""
    captured_context = []
    captured_data = []

    class CapturePolicyGate:
        async def intercept_input(self, data: str, context: PolicyContext) -> PolicyDecision:
            captured_context.append(context)
            captured_data.append(data)
            return PolicyDecision(action="allow")

    hub = TermHub(policy_gate=CapturePolicyGate())
    ws = AsyncMock()
    # Mocking principal in ws.state
    ws.state = MagicMock()
    principal = MagicMock()
    principal.subject_id = "user-123"
    ws.state.uterm_principal = principal

    worker_ws = AsyncMock()
    worker_id = "w1"
    await hub.register_worker(worker_id, worker_ws)
    await hub.register_browser(worker_id, ws, "operator")

    # Switch to open mode so operator can send input without hijack
    await hub.set_input_mode(worker_id, "open")

    msg = {"type": "input", "data": "ls -la\n"}
    await _handle_input(hub, ws, worker_id, msg)

    assert len(captured_context) == 1
    ctx = captured_context[0]
    assert ctx.worker_id == worker_id
    assert ctx.client_id == "user-123"
    assert ctx.role == "operator"
    assert ctx.action == "input"
    assert ctx.metadata == {"principal": principal}
    assert captured_data == ["ls -la\n"]


@pytest.mark.asyncio
async def test_policy_context_principal_string() -> None:
    """PolicyContext correctly handles string-based principals (no subject_id)."""
    captured_context = []

    class CapturePolicyGate:
        async def intercept_input(self, _data: str, context: PolicyContext) -> PolicyDecision:
            captured_context.append(context)
            return PolicyDecision(action="allow")

    hub = TermHub(policy_gate=CapturePolicyGate())
    ws = AsyncMock()
    ws.state = MagicMock()
    ws.state.uterm_principal = "simple-user-id"

    ctx = await hub.prepare_policy_context(ws, "w1", action="test")
    assert ctx.client_id == "simple-user-id"
