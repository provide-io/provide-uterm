#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.control_channel import ControlFrameDecoder, DataChunk
from provide.uterm.server.bridge.hub import PolicyContext, PolicyDecision, TermHub
from provide.uterm.server.bridge.routes.browser_handlers import _handle_input


@pytest.mark.asyncio
async def test_approval_flow_buffering_and_hold() -> None:
    # Setup hub with a policy gate that returns 'hold'
    class HoldPolicyGate:
        async def intercept_input(self, data: str, context: PolicyContext) -> PolicyDecision:
            if "rm -rf" in data:
                return PolicyDecision(action="hold", request_id="req-123")
            return PolicyDecision(action="allow")

    hub = TermHub(policy_gate=HoldPolicyGate())
    # Mocking approval store as it's not yet in TermHub.__init__
    hub.approval_store = MagicMock()

    ws = AsyncMock()
    worker_ws = AsyncMock()
    worker_id = "w1"

    await hub.register_worker(worker_id, worker_ws)
    await hub.register_browser(worker_id, ws, "admin")
    await hub.try_acquire_ws_hijack(worker_id, ws)

    # 1. Send "rm" - should be buffered, no policy check, no worker send
    msg = {"type": "input", "data": "rm"}
    await _handle_input(hub, ws, worker_id, msg)
    worker_ws.send_text.assert_not_called()

    # 2. Send " -rf /\n" - should complete command, trigger hold policy
    msg = {"type": "input", "data": " -rf /\n"}
    await _handle_input(hub, ws, worker_id, msg)

    # Still not sent to worker because of 'hold'
    worker_ws.send_text.assert_not_called()

    # ApprovalRequest should be created (we'll implement this)
    hub.approval_store.add.assert_called()

    # Check broadcast
    found_pending = False
    decoder = ControlFrameDecoder()
    for call in ws.send_text.call_args_list:
        payload = call[0][0]
        events = decoder.feed(payload)
        for event in events:
            if event.kind == "control" and event.control.get("type") == "approval_pending":
                found_pending = True
                assert "rm -rf /" in event.control.get("command")
                assert event.control.get("request_id") == "req-123"
    assert found_pending


@pytest.mark.asyncio
async def test_resolve_approval_approved() -> None:
    from provide.uterm.server.bridge.hub.approvals import ApprovalRequest, ApprovalStatus

    hub = TermHub()
    worker_ws = AsyncMock()
    worker_id = "w1"
    await hub.register_worker(worker_id, worker_ws)

    ws = AsyncMock()
    await hub.register_browser(worker_id, ws, "admin")
    assert await hub.try_acquire_ws_hijack(worker_id, ws) == (True, None)

    # Mock approval request
    request_id = "req-123"
    command = "ls -la\n"
    generation = await hub.capture_browser_ownership(worker_id, ws)
    assert generation is not None
    hub.approval_store.add(
        ApprovalRequest(
            id=request_id,
            worker_id=worker_id,
            submitter_id="admin",
            command=command,
            status=ApprovalStatus.PENDING,
            created_at=time.time(),
            expires_at=time.time() + 60,
            origin_browser=ws,
            ownership_generation=generation,
        )
    )

    delivered, reason = await hub.resolve_approval(worker_id, request_id, PolicyDecision(action="allow"), command)
    assert (delivered, reason) == (True, None)

    # Should be sent to worker
    worker_ws.send_text.assert_called()
    payload = worker_ws.send_text.call_args[0][0]
    decoder = ControlFrameDecoder()
    events = decoder.feed(payload)
    chunks = [e.data for e in events if isinstance(e, DataChunk)]
    assert chunks == ["ls -la\n"]

    # Should broadcast approval_resolved
    found_resolved = False
    decoder = ControlFrameDecoder()
    for call in ws.send_text.call_args_list:
        payload = call[0][0]
        events = decoder.feed(payload)
        for event in events:
            if event.kind == "control" and event.control.get("type") == "approval_resolved":
                found_resolved = True
                assert event.control.get("outcome") == "approved"
                assert event.control.get("request_id") == "req-123"
    assert found_resolved
