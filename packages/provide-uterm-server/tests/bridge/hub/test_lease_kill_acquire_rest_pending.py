#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Regression + mutation-killing tests for the two-phase REST-acquire reservation.

``HijackLeaseManager.try_acquire_rest`` reserves the slot under the hub lock
(``hijack_pending``), pauses the worker OUTSIDE the lock, then finalises the lease
under the lock. The whole point is that the global hub lock is NOT held across the
worker-pause ``send_text`` (a backpressured worker would otherwise stall every other
hub operation). These tests pin:

* the hub lock is free while the pause send is in flight (the fix itself),
* the reservation makes concurrent acquires read the slot as taken,
* the reservation is cleared on success and rolled back on send failure,
  cancellation, a vanished worker, or a superseded reservation,
* the send-failure ``worker_ws`` null only fires for the SAME socket.

The harness (``_make_manager`` / ``_make_state``) is reused from
``test_lease_kill_acquire_rest`` to mirror its ``_FakeHub`` callbacks exactly.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any
from unittest.mock import AsyncMock

from provide.uterm.server.bridge.hub.lease import HijackLeaseManager
from provide.uterm.server.bridge.hub.registry import WorkerRegistry
from provide.uterm.server.bridge.models import WorkerTermState


class _FakeHub:
    """Minimal ``_LeaseHubCallbacks`` impl (mirrors ``test_lease_kill_acquire_rest``)."""

    def __init__(self) -> None:
        self._mgr: HijackLeaseManager | None = None

    def is_hijacked(self, st: WorkerTermState) -> bool:
        return self.is_dashboard_hijack_active(st) or self.has_valid_rest_lease(st)

    def is_dashboard_hijack_active(self, st: WorkerTermState) -> bool:
        return st.hijack_owner is not None and (
            st.hijack_owner_expires_at is None or st.hijack_owner_expires_at > time.monotonic()
        )

    def has_valid_rest_lease(self, st: WorkerTermState) -> bool:
        return st.hijack_session is not None and st.hijack_session.lease_expires_at > time.monotonic()

    async def send_worker(self, worker_id: str, msg: dict[str, Any], *, source: Any = None) -> bool:
        return True


def _make_state() -> WorkerTermState:
    """A registered worker state with a live worker_ws (AsyncMock)."""
    st = WorkerTermState()
    st.worker_ws = AsyncMock()
    return st


def _make_manager() -> tuple[HijackLeaseManager, WorkerRegistry, _FakeHub, asyncio.Lock]:
    registry = WorkerRegistry()
    lock = asyncio.Lock()
    hub = _FakeHub()
    mgr = HijackLeaseManager(registry=registry, lock=lock, dashboard_hijack_lease_s=45, hub=hub)
    hub._mgr = mgr
    return mgr, registry, hub, lock


async def _blocking_send_state() -> tuple[WorkerTermState, asyncio.Event, asyncio.Event]:
    """Build a worker state whose pause ``send_text`` blocks until released.

    Returns ``(st, started, release)``: the send sets ``started`` when entered and
    awaits ``release`` before returning.
    """
    st = _make_state()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_send(_payload: str) -> None:
        started.set()
        await release.wait()

    st.worker_ws.send_text = AsyncMock(side_effect=_blocking_send)  # type: ignore[union-attr]
    return st, started, release


class TestLockReleasedDuringPause:
    """The fix: the hub lock is NOT held while the pause send is in flight."""

    async def test_lock_not_held_during_pause_send(self) -> None:
        mgr, registry, _hub, lock = _make_manager()
        st, started, release = await _blocking_send_state()
        registry.put("w1", st)

        task = asyncio.create_task(mgr.try_acquire_rest("w1", owner="op", lease_s=60, hijack_id="h", now=1.0))
        await asyncio.wait_for(started.wait(), timeout=1.0)

        # The send is mid-flight: the global lock MUST be free, and the slot reserved.
        assert lock.locked() is False
        assert st.hijack_pending == "h"

        release.set()
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result == (True, None)
        assert st.hijack_session is not None
        assert st.hijack_session.hijack_id == "h"
        assert st.hijack_pending is None


class TestReservationBlocksConcurrent:
    """A live reservation makes a concurrent acquire fail with ``already_hijacked``."""

    async def test_pending_blocks_second_acquire(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st, started, release = await _blocking_send_state()
        registry.put("w1", st)

        first = asyncio.create_task(mgr.try_acquire_rest("w1", owner="a", lease_s=60, hijack_id="h1", now=1.0))
        await asyncio.wait_for(started.wait(), timeout=1.0)

        # Second acquire while the first holds the reservation (no session yet).
        second = await mgr.try_acquire_rest("w1", owner="b", lease_s=60, hijack_id="h2", now=2.0)
        assert second == (False, "already_hijacked")

        release.set()
        assert await asyncio.wait_for(first, timeout=1.0) == (True, None)
        assert st.hijack_session is not None
        assert st.hijack_session.hijack_id == "h1"


class TestSuccessClearsReservation:
    async def test_success_clears_pending(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        registry.put("w1", st)
        ok, err = await mgr.try_acquire_rest("w1", owner="op", lease_s=60, hijack_id="h", now=1.0)
        assert (ok, err) == (True, None)
        assert st.hijack_pending is None


class TestSendFailureRollback:
    """Send failure rolls the reservation back and nulls the dead socket."""

    async def test_send_failure_clears_pending_and_worker(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.worker_ws.send_text = AsyncMock(side_effect=RuntimeError("dead"))  # type: ignore[union-attr]
        registry.put("w1", st)
        result = await mgr.try_acquire_rest("w1", owner="op", lease_s=60, hijack_id="h", now=1.0)
        assert result == (False, "no_worker")
        assert st.hijack_pending is None
        assert st.worker_ws is None
        assert st.hijack_session is None

    async def test_failed_acquire_does_not_block_reacquire(self) -> None:
        """A rolled-back reservation leaves the worker acquirable again."""
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.worker_ws.send_text = AsyncMock(side_effect=RuntimeError("dead"))  # type: ignore[union-attr]
        registry.put("w1", st)
        await mgr.try_acquire_rest("w1", owner="op", lease_s=60, hijack_id="h1", now=1.0)

        st.worker_ws = AsyncMock()  # worker reconnects
        ok, err = await mgr.try_acquire_rest("w1", owner="op2", lease_s=60, hijack_id="h2", now=2.0)
        assert (ok, err) == (True, None)
        assert st.hijack_session is not None
        assert st.hijack_session.hijack_id == "h2"

    async def test_send_failure_after_reconnect_preserves_new_ws(self) -> None:
        """The dead-socket null only fires for the SAME ws (covers ``is worker_ws``)."""
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        new_ws = AsyncMock()

        async def _reconnect_then_fail(_payload: str) -> None:
            st.worker_ws = new_ws  # a fresh worker connected mid-send
            raise RuntimeError("old socket dead")

        st.worker_ws.send_text = AsyncMock(side_effect=_reconnect_then_fail)  # type: ignore[union-attr]
        registry.put("w1", st)
        result = await mgr.try_acquire_rest("w1", owner="op", lease_s=60, hijack_id="h", now=1.0)
        assert result == (False, "no_worker")
        assert st.worker_ws is new_ws  # the fresh socket must NOT be nulled
        assert st.hijack_pending is None
        assert st.hijack_session is None

    async def test_send_failure_after_worker_removed_is_noop(self) -> None:
        """Send fails AND the worker is gone → no null/clear to do (``st is not None`` False)."""
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()

        async def _drop_then_fail(_payload: str) -> None:
            registry._workers.pop("w1", None)
            raise RuntimeError("dead")

        st.worker_ws.send_text = AsyncMock(side_effect=_drop_then_fail)  # type: ignore[union-attr]
        registry.put("w1", st)
        result = await mgr.try_acquire_rest("w1", owner="op", lease_s=60, hijack_id="h", now=1.0)
        assert result == (False, "no_worker")
        assert registry._workers.get("w1") is None
        assert st.hijack_session is None


class TestWorkerVanishedOrSupersededMidSend:
    """Phase-3 / finally edge branches when state changes during the pause send."""

    async def test_worker_removed_during_send_returns_no_worker(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()

        async def _drop_worker(_payload: str) -> None:
            registry._workers.pop("w1", None)  # worker fully gone, send still "succeeds"

        st.worker_ws.send_text = AsyncMock(side_effect=_drop_worker)  # type: ignore[union-attr]
        registry.put("w1", st)
        result = await mgr.try_acquire_rest("w1", owner="op", lease_s=60, hijack_id="h", now=1.0)
        assert result == (False, "no_worker")  # phase-3 ``st is None``
        assert st.hijack_session is None

    async def test_pending_superseded_during_send_returns_no_worker(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()

        async def _supersede(_payload: str) -> None:
            st.hijack_pending = "other"  # reservation changed out from under us

        st.worker_ws.send_text = AsyncMock(side_effect=_supersede)  # type: ignore[union-attr]
        registry.put("w1", st)
        result = await mgr.try_acquire_rest("w1", owner="op", lease_s=60, hijack_id="h", now=1.0)
        assert result == (False, "no_worker")  # phase-3 ``hijack_pending != hijack_id``
        assert st.hijack_session is None
        assert st.hijack_pending == "other"  # finally must NOT clear another's reservation


class TestCancellationRollback:
    async def test_cancel_mid_send_rolls_back_reservation(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st, started, _release = await _blocking_send_state()
        registry.put("w1", st)

        task = asyncio.create_task(mgr.try_acquire_rest("w1", owner="op", lease_s=60, hijack_id="h", now=1.0))
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert st.hijack_pending == "h"

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert st.hijack_pending is None  # finally rolled it back — worker stays acquirable
        assert st.hijack_session is None
