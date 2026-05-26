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


# ---------------------------------------------------------------------------
# Concurrency tests for the threading-lock-protected store.
# ---------------------------------------------------------------------------


def test_concurrent_resolve_converges_to_one_winner() -> None:
    """Multiple threads racing ``resolve`` on the same request must agree.

    Only the first status to win the lock takes effect, because ``resolve``
    only mutates while ``status == PENDING``. All threads see the same final
    state when they finish.
    """
    import threading

    store = InMemoryApprovalStore()
    now = time.time()
    request = ApprovalRequest("req-race", "w1", "u1", "cmd", ApprovalStatus.PENDING, now, now + 60)
    store.add(request)

    barrier = threading.Barrier(8)
    results: list[ApprovalStatus] = []

    def resolver(target: ApprovalStatus) -> None:
        barrier.wait()
        store.resolve("req-race", target)
        results.append(store.get("req-race").status)

    threads = [
        threading.Thread(target=resolver, args=(ApprovalStatus.APPROVED if i % 2 == 0 else ApprovalStatus.REJECTED,))
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = store.get("req-race").status
    assert final in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}
    # All resolvers observe the same terminal state once they're past their own resolve.
    assert all(r == final for r in results)


@pytest.mark.asyncio
async def test_concurrent_add_and_cleanup_do_not_corrupt() -> None:
    """``add`` from threads while ``cleanup_expired`` runs must not crash."""
    import threading

    store = InMemoryApprovalStore()
    now = time.time()
    # Seed with a mix of expired-pending + still-pending entries.
    for i in range(20):
        store.add(
            ApprovalRequest(
                f"old-{i}", "w1", "u1", "cmd", ApprovalStatus.PENDING, now - 100, now - 1 if i % 2 else now + 100
            )
        )

    stop = threading.Event()

    def adder() -> None:
        i = 0
        while not stop.is_set():
            store.add(ApprovalRequest(f"new-{i}", "w1", "u1", "cmd", ApprovalStatus.PENDING, now, now + 1000))
            i += 1

    threads = [threading.Thread(target=adder) for _ in range(4)]
    for t in threads:
        t.start()

    # Run a few cleanup passes concurrently.
    for _ in range(5):
        await store.cleanup_expired()

    stop.set()
    for t in threads:
        t.join()

    # Spot-check: every pre-seeded "expired pending" entry was timed out.
    for i in range(20):
        req = store.get(f"old-{i}")
        assert req is not None
        if i % 2 == 1:  # expired
            assert req.status == ApprovalStatus.TIMEOUT
        else:
            assert req.status == ApprovalStatus.PENDING


@pytest.mark.asyncio
async def test_cleanup_invokes_on_expired_outside_lock() -> None:
    """``on_expired`` is invoked for every PENDING entry that just expired."""
    store = InMemoryApprovalStore()
    now = time.time()
    store.add(ApprovalRequest("e1", "w1", "u1", "cmd", ApprovalStatus.PENDING, now - 100, now - 1))
    store.add(ApprovalRequest("e2", "w1", "u1", "cmd", ApprovalStatus.PENDING, now - 100, now - 1))

    expired_ids: list[str] = []
    store.on_expired = expired_ids.append

    await store.cleanup_expired()
    assert sorted(expired_ids) == ["e1", "e2"]
    assert store.get("e1").status == ApprovalStatus.TIMEOUT
    assert store.get("e2").status == ApprovalStatus.TIMEOUT
