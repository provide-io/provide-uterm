#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from provide.uterm.bridge.hub.approvals import ApprovalRequest, ApprovalStatus, InMemoryApprovalStore
from provide.uterm.bridge.hub.core import TermHub


@pytest.mark.asyncio
async def test_approvals_pruning():
    store = InMemoryApprovalStore()
    now = time.time()

    # 1. Pending request - should NOT be pruned (only status changed to TIMEOUT)
    req_pending = ApprovalRequest("pending", "w1", "u1", "cmd", ApprovalStatus.PENDING, now - 100, now - 10)
    store.add(req_pending)

    # 2. Approved request, not yet reached PRUNE_TTL - should NOT be pruned
    req_approved = ApprovalRequest("approved", "w1", "u1", "cmd", ApprovalStatus.APPROVED, now - 100, now + 1000)
    store.add(req_approved)

    # 3. Timeout request, reached PRUNE_TTL (let's assume 1 hour TTL)
    req_prunable = ApprovalRequest("prunable", "w1", "u1", "cmd", ApprovalStatus.TIMEOUT, now - 5000, now - 4000)
    store.add(req_prunable)

    await store.cleanup_expired()

    assert store.get("pending") is not None
    assert store.get("pending").status == ApprovalStatus.TIMEOUT
    assert store.get("approved") is not None
    assert store.get("prunable") is None  # Should be pruned


@pytest.mark.asyncio
async def test_input_buffer_pruning():
    hub = TermHub()
    ws = MagicMock()

    # Simulate some buffered input
    hub._input_buffers[ws] = "partial command"

    # Simulate browser disconnect
    await hub.cleanup_browser_disconnect("worker1", ws, False)

    assert ws not in hub._input_buffers


@pytest.mark.asyncio
async def test_input_buffer_pruning_dead_browsers():
    hub = TermHub()
    ws = MagicMock()

    # Simulate some buffered input
    hub._input_buffers[ws] = "partial command"

    # Simulate dead browser removal
    await hub.remove_dead_browsers("worker1", {ws})

    assert ws not in hub._input_buffers
