#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import time

import pytest

from provide.uterm.bridge.hub.approvals import ApprovalRequest, ApprovalStatus, InMemoryApprovalStore


def test_approval_request_creation():
    now = time.time()
    request = ApprovalRequest(
        id="req-123",
        worker_id="w1",
        submitter_id="u1",
        command="ls",
        status=ApprovalStatus.PENDING,
        created_at=now,
        expires_at=now + 60,
    )
    assert request.id == "req-123"
    assert request.status == ApprovalStatus.PENDING


def test_store_add_and_get():
    store = InMemoryApprovalStore()
    now = time.time()
    request = ApprovalRequest("req-1", "w1", "u1", "cmd", ApprovalStatus.PENDING, now, now + 60)
    store.add(request)
    assert store.get("req-1") == request
    assert store.get("nonexistent") is None


def test_store_resolve_success():
    store = InMemoryApprovalStore()
    now = time.time()
    request = ApprovalRequest("req-1", "w1", "u1", "cmd", ApprovalStatus.PENDING, now, now + 60)
    store.add(request)
    store.resolve("req-1", ApprovalStatus.APPROVED)
    assert store.get("req-1").status == ApprovalStatus.APPROVED


def test_store_resolve_only_pending():
    store = InMemoryApprovalStore()
    now = time.time()
    request = ApprovalRequest("req-1", "w1", "u1", "cmd", ApprovalStatus.APPROVED, now, now + 60)
    store.add(request)
    store.resolve("req-1", ApprovalStatus.REJECTED)
    assert store.get("req-1").status == ApprovalStatus.APPROVED  # Unchanged


@pytest.mark.asyncio
async def test_cleanup_expired_requests():
    store = InMemoryApprovalStore()
    now = time.time()
    req1 = ApprovalRequest("exp", "w1", "u1", "cmd", ApprovalStatus.PENDING, now - 100, now - 10)
    req2 = ApprovalRequest("valid", "w1", "u1", "cmd", ApprovalStatus.PENDING, now - 10, now + 10)
    store.add(req1)
    store.add(req2)
    await store.cleanup_expired()
    assert store.get("exp").status == ApprovalStatus.TIMEOUT
    assert store.get("valid").status == ApprovalStatus.PENDING
