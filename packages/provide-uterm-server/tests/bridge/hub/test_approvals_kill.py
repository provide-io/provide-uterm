#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for ``approvals`` — the fencing, the clock, and the delivery.

``InMemoryApprovalStore`` is the ``approval_store`` service. CLAUDE.md lists it
among the nine hub services on the mutation perimeter; it was never in
``source_paths``, so none of it was enforced. Measured cold it killed 89 of 105
mutants, and the 16 that lived are the parts that decide whether a held command
runs.

Three clusters, none of them visible to a happy-path test:

*The revision counter.* Nothing asserted which revision a request is given, so
the counter could start at 1 or step by 2. Revisions are what
``resolve``/``claim``/``finalize`` fence on -- a caller must present the
revision it claimed -- so the numbering is the mechanism, not a detail.

*The expiry boundary, at exact equality.* An approval whose deadline is exactly
now is expired for ``claim``/``resolve``/``claim_request`` (``<=``) and is NOT
expired for ``cleanup_expired`` (``<``). That asymmetry is real, live behaviour
and it is pinned here in both directions; a test that never lands on the
boundary cannot see either side of it. These need a fixed clock, so the store's
``time`` module is replaced rather than the test sleeping.

*Delivery of expiry notifications.* ``asyncio.iscoroutine(res)`` decides whether
an async listener is awaited. Replace the argument with ``None`` and it is
always False: the coroutine is created, never awaited, and the subscriber is
simply never told. Nothing failed, because nothing asserted the listener ran.
"""

from __future__ import annotations

import pytest

from provide.uterm.server.bridge.hub import approvals as approvals_mod
from provide.uterm.server.bridge.hub.approvals import (
    ApprovalRequest,
    ApprovalStatus,
    InMemoryApprovalStore,
)

_NOW = 1_000_000.0
_PRUNE_TTL = 3600


class _FixedClock:
    """Stand-in for the ``time`` module, so boundaries land exactly."""

    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> _FixedClock:
    fixed = _FixedClock(_NOW)
    monkeypatch.setattr(approvals_mod, "time", fixed)
    return fixed


def _request(request_id: str = "r1", *, expires_at: float = _NOW + 60.0) -> ApprovalRequest:
    return ApprovalRequest(
        id=request_id,
        worker_id="w1",
        submitter_id="u1",
        command="rm -rf /",
        status=ApprovalStatus.PENDING,
        created_at=_NOW,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# The revision counter
# ---------------------------------------------------------------------------


def test_revisions_start_at_one_and_advance_by_one() -> None:
    """Revisions are the fence token; their exact values are the contract.

    A counter starting at 1 (so the first request is revision 2) or stepping by
    2 still produces distinct, increasing numbers -- every "is it unique" style
    assertion passes. A caller holding revision 1 would then be fenced out of a
    request it legitimately claimed.
    """
    store = InMemoryApprovalStore()

    store.add(_request("r1"))
    store.add(_request("r2"))

    first = store.get("r1")
    second = store.get("r2")
    assert first is not None and second is not None
    assert first.revision == 1
    assert second.revision == 2


def test_a_duplicate_id_is_refused_and_does_not_consume_a_revision() -> None:
    """Rejecting the duplicate is only half of it; it must not renumber the original."""
    store = InMemoryApprovalStore()
    store.add(_request("r1"))

    assert store.add(_request("r1")) is False

    held = store.get("r1")
    assert held is not None
    assert held.revision == 1


# ---------------------------------------------------------------------------
# The expiry boundary — exact equality
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["resolve", "claim"])
def test_an_approval_expiring_exactly_now_cannot_be_claimed(clock: _FixedClock, method: str) -> None:
    """``expires_at <= now``: the deadline is inclusive, so this must be refused.

    At ``<`` the request is still claimable on the tick it expires -- a held
    command would run on the strength of an approval that had just lapsed.
    """
    store = InMemoryApprovalStore()
    store.add(_request(expires_at=_NOW))

    granted = getattr(store, method)("r1", ApprovalStatus.APPROVED, expected_revision=1)

    assert granted is False
    lapsed = store.get("r1")
    assert lapsed is not None
    assert lapsed.status is ApprovalStatus.TIMEOUT


def test_claim_request_at_the_deadline_returns_no_snapshot(clock: _FixedClock) -> None:
    """Same boundary on the snapshot-returning path, which is what the router uses."""
    store = InMemoryApprovalStore()
    store.add(_request(expires_at=_NOW))

    assert store.claim_request("r1", ApprovalStatus.RESOLVING, expected_revision=1) is None


def test_an_approval_expiring_one_tick_later_is_still_claimable(clock: _FixedClock) -> None:
    """The near side of the same boundary, so "always refuse" cannot pass either."""
    store = InMemoryApprovalStore()
    store.add(_request(expires_at=_NOW + 0.001))

    assert store.claim("r1", ApprovalStatus.APPROVED, expected_revision=1) is True


# ---------------------------------------------------------------------------
# The fence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["resolve", "claim", "finalize"])
def test_an_unknown_request_is_refused_rather_than_raising(clock: _FixedClock, method: str) -> None:
    """``req is None or ...`` short-circuits; ``and`` would dereference the None.

    Callers treat a False return as "someone else got there first". Turning that
    into an AttributeError on a lookup miss converts an ordinary race into a
    500.
    """
    store = InMemoryApprovalStore()

    assert getattr(store, method)("nope", ApprovalStatus.APPROVED, expected_revision=1) is False


def test_an_unknown_request_yields_no_snapshot_rather_than_raising(clock: _FixedClock) -> None:
    store = InMemoryApprovalStore()

    assert store.claim_request("nope", ApprovalStatus.RESOLVING, expected_revision=1) is None


def test_a_stale_revision_cannot_finalize_a_reserved_request(clock: _FixedClock) -> None:
    """The whole point of the fence: a pruned or reused id must not take a stale write."""
    store = InMemoryApprovalStore()
    store.add(_request())
    assert store.claim_request("r1", ApprovalStatus.RESOLVING, expected_revision=1) is not None

    assert store.finalize("r1", ApprovalStatus.APPROVED, expected_revision=99) is False

    still_reserved = store.get("r1")
    assert still_reserved is not None
    assert still_reserved.status is ApprovalStatus.RESOLVING


def test_finalize_writes_the_status_it_was_given(clock: _FixedClock) -> None:
    """Returning True while leaving the status unset would strand the request."""
    store = InMemoryApprovalStore()
    store.add(_request())
    store.claim_request("r1", ApprovalStatus.RESOLVING, expected_revision=1)

    assert store.finalize("r1", ApprovalStatus.REFUSED, expected_revision=1) is True

    done = store.get("r1")
    assert done is not None
    assert done.status is ApprovalStatus.REFUSED


@pytest.mark.parametrize("status", [ApprovalStatus.PENDING, ApprovalStatus.TIMEOUT, ApprovalStatus.RESOLVING])
def test_finalizing_as_a_non_terminal_status_is_rejected_with_its_stated_reason(
    clock: _FixedClock, status: ApprovalStatus
) -> None:
    """Asserted by equality, not ``match=``.

    ``pytest.raises(match=...)`` is a regex *search*, so it still passes against
    a sentinel-wrapped literal -- the same blind spot a substring assertion has.
    """
    store = InMemoryApprovalStore()

    with pytest.raises(ValueError) as excinfo:
        store.finalize("r1", status, expected_revision=1)

    assert str(excinfo.value) == "approval resolution must finalize as approved or refused"


# ---------------------------------------------------------------------------
# cleanup_expired — the other side of the boundary, and the prune window
# ---------------------------------------------------------------------------


async def test_a_pending_request_due_exactly_now_is_not_swept(clock: _FixedClock) -> None:
    """``cleanup_expired`` uses ``<``, so equality is NOT expired here.

    That is the opposite of the claim paths above, which use ``<=``. The
    asymmetry is the live behaviour; pinning both sides is what makes a change
    to either one visible.
    """
    store = InMemoryApprovalStore()
    store.add(_request(expires_at=_NOW))

    await store.cleanup_expired()

    survived = store.get("r1")
    assert survived is not None
    assert survived.status is ApprovalStatus.PENDING


async def test_a_pending_request_past_its_deadline_is_swept(clock: _FixedClock) -> None:
    store = InMemoryApprovalStore()
    store.add(_request(expires_at=_NOW - 0.001))

    await store.cleanup_expired()

    swept = store.get("r1")
    assert swept is not None
    assert swept.status is ApprovalStatus.TIMEOUT


async def test_a_terminal_request_is_pruned_only_after_the_full_hour(clock: _FixedClock) -> None:
    """The prune window is 3600s past expiry, and the test straddles it.

    Checked just inside and just past the window, so neither widening the TTL by
    a second nor flipping ``<`` to ``<=`` survives.
    """
    store = InMemoryApprovalStore()
    store.add(_request(expires_at=_NOW))
    store.claim("r1", ApprovalStatus.APPROVED, expected_revision=1)

    clock.now = _NOW + _PRUNE_TTL
    await store.cleanup_expired()
    assert store.get("r1") is not None, "pruned exactly on the window edge"

    clock.now = _NOW + _PRUNE_TTL + 0.5
    await store.cleanup_expired()
    assert store.get("r1") is None, "still held half a second past the window"


# ---------------------------------------------------------------------------
# notify_expired — the delivery
# ---------------------------------------------------------------------------


async def test_an_async_subscriber_is_actually_awaited(clock: _FixedClock) -> None:
    """``iscoroutine(res)`` gates the await; with ``None`` it is always False.

    The coroutine is still created, so nothing raises -- the subscriber is just
    never run, and an expiry goes unannounced.
    """
    store = InMemoryApprovalStore()
    seen: list[str] = []

    async def _listener(request: ApprovalRequest) -> None:
        seen.append(request.id)

    store.subscribe_expired(_listener)
    store.add(_request(expires_at=_NOW - 1.0))

    await store.cleanup_expired()

    assert seen == ["r1"]


async def test_the_legacy_async_hook_is_also_awaited(clock: _FixedClock) -> None:
    """``on_expired`` is a second call site with its own ``iscoroutine`` check."""
    store = InMemoryApprovalStore()
    seen: list[str] = []

    async def _hook(request_id: str) -> None:
        seen.append(request_id)

    store.on_expired = _hook
    store.add(_request(expires_at=_NOW - 1.0))

    await store.cleanup_expired()

    assert seen == ["r1"]


async def test_a_synchronous_subscriber_still_runs(clock: _FixedClock) -> None:
    """The non-coroutine branch has to keep working; awaiting it would raise."""
    store = InMemoryApprovalStore()
    seen: list[str] = []

    store.subscribe_expired(lambda request: seen.append(request.id))
    store.add(_request(expires_at=_NOW - 1.0))

    await store.cleanup_expired()

    assert seen == ["r1"]


async def test_an_expiry_is_announced_once_and_not_again(clock: _FixedClock) -> None:
    """Queued snapshots are cleared under the lock; a repeat sweep must stay quiet."""
    store = InMemoryApprovalStore()
    seen: list[str] = []
    store.subscribe_expired(lambda request: seen.append(request.id))
    store.add(_request(expires_at=_NOW - 1.0))

    await store.cleanup_expired()
    await store.cleanup_expired()

    assert seen == ["r1"]
