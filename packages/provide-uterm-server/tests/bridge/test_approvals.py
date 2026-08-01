#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import time

import pytest

from provide.uterm.server.bridge.hub.approvals import ApprovalRequest, ApprovalStatus, InMemoryApprovalStore


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
    assert store.add(request) is True
    stored = store.get("req-1")
    assert stored is not None
    assert stored == ApprovalRequest(**{**request.__dict__, "revision": stored.revision})
    assert request.revision == 0
    assert store.get("nonexistent") is None


def test_store_rejects_duplicate_request_id_without_replacing_original() -> None:
    store = InMemoryApprovalStore()
    now = time.time()
    original = ApprovalRequest("same", "w1", "u1", "first", ApprovalStatus.PENDING, now, now + 60)
    replacement = ApprovalRequest("same", "w2", "u2", "second", ApprovalStatus.PENDING, now, now + 60)

    assert store.add(original) is True
    assert store.add(replacement) is False

    stored = store.get("same")
    assert stored is not None
    assert (stored.worker_id, stored.command) == ("w1", "first")


def test_store_get_and_pending_return_immutable_snapshots() -> None:
    store = InMemoryApprovalStore()
    now = time.time()
    request = ApprovalRequest("r1", "w1", "u1", "safe", ApprovalStatus.PENDING, now, now + 60)
    assert store.add(request) is True

    fetched = store.get("r1")
    assert fetched is not None
    fetched.command = "mutated"
    fetched.status = ApprovalStatus.REJECTED
    listed = store.pending()
    assert len(listed) == 1
    listed[0].command = "also-mutated"

    stored = store.get("r1")
    assert stored is not None
    assert (stored.command, stored.status) == ("safe", ApprovalStatus.PENDING)


def test_claimed_revision_cannot_finalize_a_replacement() -> None:
    store = InMemoryApprovalStore()
    now = time.time()
    assert store.add(ApprovalRequest("r1", "w1", "u1", "first", ApprovalStatus.PENDING, now, now + 60))
    snapshot = store.get("r1")
    assert snapshot is not None
    claimed = store.claim_request("r1", ApprovalStatus.RESOLVING, expected_revision=snapshot.revision)
    assert claimed is not None

    # A stale/mismatched revision cannot complete the active claim.
    assert store.finalize("r1", ApprovalStatus.APPROVED, expected_revision=claimed.revision + 1) is False
    assert store.finalize("r1", ApprovalStatus.APPROVED, expected_revision=claimed.revision) is True


def test_stale_claim_cannot_mutate_reused_request_id() -> None:
    store = InMemoryApprovalStore()
    now = time.time()
    assert store.add(ApprovalRequest("r1", "w1", "u1", "first", ApprovalStatus.PENDING, now, now + 60))
    first = store.get("r1")
    assert first is not None
    claimed = store.claim_request("r1", ApprovalStatus.RESOLVING, expected_revision=first.revision)
    assert claimed is not None

    # Deterministically model terminal pruning followed by request-ID reuse.
    with store._lock:
        del store._requests["r1"]
    assert store.add(ApprovalRequest("r1", "w2", "u2", "second", ApprovalStatus.PENDING, now, now + 60))
    replacement = store.get("r1")
    assert replacement is not None
    assert replacement.revision > claimed.revision

    assert store.finalize("r1", ApprovalStatus.APPROVED, expected_revision=claimed.revision) is False
    assert store.resolve("r1", ApprovalStatus.REJECTED, expected_revision=claimed.revision) is False
    assert store.claim_request("r1", ApprovalStatus.REJECTED, expected_revision=claimed.revision) is None
    assert store.get("r1") == replacement


def test_claim_request_returns_a_snapshot() -> None:
    store = InMemoryApprovalStore()
    now = time.time()
    assert store.add(ApprovalRequest("r1", "w1", "u1", "safe", ApprovalStatus.PENDING, now, now + 60))
    pending = store.get("r1")
    assert pending is not None
    claimed = store.claim_request("r1", ApprovalStatus.REJECTED, expected_revision=pending.revision)
    assert claimed is not None
    claimed.command = "mutated"
    stored = store.get("r1")
    assert stored is not None
    assert (stored.command, stored.status) == ("safe", ApprovalStatus.REJECTED)


@pytest.mark.asyncio
async def test_expired_request_is_timed_out_atomically_instead_of_claimed() -> None:
    store = InMemoryApprovalStore()
    now = time.time()
    assert store.add(ApprovalRequest("expired", "w1", "u1", "unsafe", ApprovalStatus.PENDING, now - 2, now - 1))
    pending = store.get("expired")
    assert pending is not None
    expired: list[ApprovalRequest] = []
    store.subscribe_expired(expired.append)

    claimed = store.claim_request("expired", ApprovalStatus.RESOLVING, expected_revision=pending.revision)
    await store.notify_expired()

    assert claimed is None
    stored = store.get("expired")
    assert stored is not None
    assert stored.status == ApprovalStatus.TIMEOUT
    assert expired == [stored]


@pytest.mark.asyncio
async def test_expiration_subscribers_are_composed_and_receive_snapshots() -> None:
    store = InMemoryApprovalStore()
    now = time.time()
    assert store.add(ApprovalRequest("expired", "w1", "u1", "safe", ApprovalStatus.PENDING, now - 2, now - 1))
    first: list[ApprovalRequest] = []
    second: list[ApprovalRequest] = []
    store.subscribe_expired(first.append)
    store.subscribe_expired(second.append)

    await store.cleanup_expired()

    assert len(first) == len(second) == 1
    assert first[0] == second[0]
    first[0].command = "mutated snapshot"
    stored = store.get("expired")
    assert stored is not None
    assert stored.command == "safe"


@pytest.mark.parametrize("transition", ["resolve", "claim"])
def test_all_direct_transitions_fail_closed_after_expiration(transition: str) -> None:
    store = InMemoryApprovalStore()
    now = time.time()
    assert store.add(ApprovalRequest("expired", "w1", "u1", "unsafe", ApprovalStatus.PENDING, now - 2, now - 1))
    pending = store.get("expired")
    assert pending is not None

    changed = getattr(store, transition)("expired", ApprovalStatus.APPROVED, expected_revision=pending.revision)

    assert changed is False
    stored = store.get("expired")
    assert stored is not None
    assert stored.status == ApprovalStatus.TIMEOUT


def test_store_resolve_success():
    store = InMemoryApprovalStore()
    now = time.time()
    request = ApprovalRequest("req-1", "w1", "u1", "cmd", ApprovalStatus.PENDING, now, now + 60)
    store.add(request)
    stored = store.get("req-1")
    assert stored is not None
    assert store.resolve("req-1", ApprovalStatus.APPROVED, expected_revision=stored.revision) is True
    assert store.get("req-1").status == ApprovalStatus.APPROVED


def test_store_resolve_only_pending():
    store = InMemoryApprovalStore()
    now = time.time()
    request = ApprovalRequest("req-1", "w1", "u1", "cmd", ApprovalStatus.APPROVED, now, now + 60)
    store.add(request)
    stored = store.get("req-1")
    assert stored is not None
    assert store.resolve("req-1", ApprovalStatus.REJECTED, expected_revision=stored.revision) is False
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
    stored = store.get("req-race")
    assert stored is not None
    revision = stored.revision

    barrier = threading.Barrier(8)
    results: list[ApprovalStatus] = []

    def resolver(target: ApprovalStatus) -> None:
        barrier.wait()
        store.resolve("req-race", target, expected_revision=revision)
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
