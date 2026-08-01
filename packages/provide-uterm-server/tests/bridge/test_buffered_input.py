#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for buffered input state machine during pending approvals."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.hub.approvals import ApprovalRequest, ApprovalStatus
from provide.uterm.server.bridge.hub.ext import PolicyDecision
from provide.uterm.server.bridge.models import WorkerTermState
from provide.uterm.server.bridge.routes.browser_handlers import handle_browser_message


def _make_hub() -> TermHub:
    hub = TermHub()
    # Mock handle_browser_message callback for playback
    hub._on_browser_message = handle_browser_message
    return hub


def _make_ws() -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


def _make_worker_ws() -> MagicMock:
    wws = MagicMock()
    wws.send_text = AsyncMock()
    return wws


async def _register(
    hub: TermHub, worker_id: str, browser_ws: MagicMock, role: str, worker_ws: MagicMock | None = None
) -> None:
    async with hub._lock:
        st = hub.registry._workers.setdefault(worker_id, WorkerTermState())
        st.browsers[browser_ws] = role
        if worker_ws is not None:
            st.worker_ws = worker_ws


@pytest.mark.asyncio
async def test_input_buffering_when_paused() -> None:
    hub = _make_hub()
    ws = _make_ws()
    worker_ws = _make_worker_ws()
    await _register(hub, "w1", ws, "operator", worker_ws)

    # Simulate browser being paused (e.g. pending approval)
    hub._paused_browsers.add(ws)

    # Send input
    await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "echo "}, False)
    await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "hello\r"}, False)

    # Verify nothing was sent to worker
    worker_ws.send_text.assert_not_called()

    # Verify data is in hold buffer
    assert hub._hold_buffers.get(ws) == "echo hello\r"


@pytest.mark.asyncio
async def test_input_playback_on_approval_resolve() -> None:
    hub = _make_hub()
    ws = _make_ws()
    worker_ws = _make_worker_ws()
    await _register(hub, "w1", ws, "operator", worker_ws)

    # Set input mode to hijack (default)
    async with hub._lock:
        hub.registry._workers["w1"].input_mode = "hijack"
        hub.registry._workers["w1"].hijack_owner = ws
        hub.registry._workers["w1"].hijack_owner_expires_at = time.monotonic() + 60

    # Simulate browser being paused and having buffered data
    hub._paused_browsers.add(ws)
    hub._hold_buffers[ws] = "ls\r"
    generation = await hub.capture_browser_ownership("w1", ws)
    assert generation is not None
    hub.approval_store.add(
        ApprovalRequest(
            id="req1",
            worker_id="w1",
            submitter_id="operator",
            command="sudo rm -rf /\r",
            status=ApprovalStatus.PENDING,
            created_at=time.time(),
            expires_at=time.time() + 60,
            origin_browser=ws,
            ownership_generation=generation,
        )
    )

    # Resolve approval
    decision = PolicyDecision(action="allow")
    await hub.resolve_approval("w1", "req1", decision, "sudo rm -rf /\r")

    # Verify approved command sent to worker
    # We might need to wait for the background task
    for _ in range(10):
        if worker_ws.send_text.call_count >= 2:
            break
        await asyncio.sleep(0.01)

    assert worker_ws.send_text.call_count >= 2
    # First call is the approved command "sudo rm -rf /\r"
    # Second call is the buffered command "ls\r"
    calls = [call[0][0] for call in worker_ws.send_text.call_args_list]
    assert "sudo rm -rf /\r" in calls
    assert "ls\r" in calls


@pytest.mark.asyncio
async def test_hold_buffer_cleanup_on_disconnect() -> None:
    hub = _make_hub()
    ws = _make_ws()
    await _register(hub, "w1", ws, "operator")

    hub._hold_buffers[ws] = "secret"

    await hub.cleanup_browser_disconnect("w1", ws, False)

    assert ws not in hub._hold_buffers
