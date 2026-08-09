#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for the ownership fence added by the lifecycle-fencing rework.

That change gave seven :class:`HijackLeaseManager` methods the same three-part
shape, and the existing suites assert none of the three:

1. **Identity recheck.** State is captured under the global lock, the per-worker
   ``owned_input_fence`` is awaited, then the registry is re-read and compared
   with ``st is not state``. No suite ever swaps the registry entry while the
   fence is held, so both the branch and its ``(False, "no_worker")`` return are
   unobserved. :class:`_ReplacingFence` performs that swap.

2. **``ownership_generation += 1``.** Every mutation of the increment (``= 1``,
   ``-= 1``, ``+= 2``) survives a test that only checks the return value. Each
   test here seeds a distinctive non-zero generation and asserts the exact
   successor, which kills all three forms in one assertion — ``= 1`` and
   ``-= 1`` differ in value, ``+= 2`` differs in step.

3. **The bounded-send timeout.** ``timeout=_OWNED_INPUT_SEND_TIMEOUT_S`` mutates
   to ``timeout=None``, turning a bounded worker send into an unbounded one — a
   backpressured worker would then stall the caller forever. Asserting the
   literal ``5.0`` reached ``asyncio.wait_for`` pins both the argument and the
   constant. Deliberately spied rather than provoked with a real hang: an actual
   hang would register as a mutmut ``timeout``, which the gate counts as unkilled
   unless allowlisted.

Harness (``_FakeHub``, ``_make_state``, ``_make_manager``) mirrors ``test_lease.py``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, patch

from provide.uterm.server.bridge.hub.lease import HijackLeaseManager
from provide.uterm.server.bridge.hub.registry import WorkerRegistry
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

# A seeded generation that no mutation can coincidentally reproduce: ``= 1``
# lands on 1, ``-= 1`` on 6, ``+= 2`` on 9, and only the real ``+= 1`` on 8.
_SEED_GENERATION = 7
_NEXT_GENERATION = 8


class _FakeHub:
    """Minimal ``_LeaseHubCallbacks`` impl (mirrors ``test_lease.py``)."""

    def __init__(self) -> None:
        self.send_worker_calls: list[tuple[str, dict[str, Any]]] = []
        self.notify_calls: list[tuple[str, bool, str | None]] = []
        self.broadcast_calls: list[str] = []
        self.events: list[tuple[str, str]] = []
        self.metrics: list[str] = []
        self.prune_calls: list[str] = []
        self.send_worker_result = True
        self.send_worker_exc: BaseException | None = None
        self._mgr: HijackLeaseManager | None = None

    def is_hijacked(self, st: WorkerTermState) -> bool:
        return self.is_dashboard_hijack_active(st) or self.has_valid_rest_lease(st)

    def is_dashboard_hijack_active(self, st: WorkerTermState) -> bool:
        return st.hijack_owner is not None and (
            st.hijack_owner_expires_at is None or st.hijack_owner_expires_at > time.monotonic()
        )

    def has_valid_rest_lease(self, st: WorkerTermState) -> bool:
        return st.hijack_session is not None and st.hijack_session.lease_expires_at > time.monotonic()

    def can_send_input(self, st: WorkerTermState, ws: Any) -> bool:
        return st.hijack_owner is ws or st.input_mode == "open"

    def metric(self, name: str, value: int = 1) -> None:
        self.metrics.append(name)

    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None = None) -> None:
        self.notify_calls.append((worker_id, enabled, owner))

    async def send_worker(
        self,
        worker_id: str,
        msg: dict[str, Any],
        *,
        source: Any = None,
        expected_worker: Any = None,
    ) -> bool:
        self.send_worker_calls.append((worker_id, msg))
        if self.send_worker_exc is not None:
            raise self.send_worker_exc
        return self.send_worker_result

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        self.broadcast_calls.append(worker_id)

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self.events.append((worker_id, event_type))
        return {}

    async def prune_if_idle(self, worker_id: str) -> None:
        self.prune_calls.append(worker_id)

    async def _recheck_and_resume(self, worker_id: str, now: float) -> None:
        if self._mgr is not None:
            await self._mgr._recheck_and_resume(worker_id, now)


class _ReplacingFence:
    """Fence that swaps the registry entry the moment it is entered.

    Models the race the identity recheck exists for: the worker reconnected (or
    was evicted and re-registered) while this coroutine waited on the fence, so
    the state captured before the wait is stale and must not be mutated.
    """

    def __init__(self, registry: WorkerRegistry, worker_id: str, replacement: WorkerTermState) -> None:
        self._registry = registry
        self._worker_id = worker_id
        self._replacement = replacement

    async def __aenter__(self) -> None:
        self._registry.put(self._worker_id, self._replacement)

    async def __aexit__(self, *_args: Any) -> None:
        return None


def _make_state() -> WorkerTermState:
    """A registered worker state with a live worker_ws and a seeded generation."""
    st = WorkerTermState()
    st.worker_ws = AsyncMock()
    st.ownership_generation = _SEED_GENERATION
    return st


def _make_manager(*, dashboard_hijack_lease_s: int = 45) -> tuple[HijackLeaseManager, WorkerRegistry, _FakeHub]:
    registry = WorkerRegistry()
    hub = _FakeHub()
    mgr = HijackLeaseManager(
        registry=registry,
        lock=asyncio.Lock(),
        dashboard_hijack_lease_s=dashboard_hijack_lease_s,
        hub=hub,
    )
    hub._mgr = mgr
    return mgr, registry, hub


def _rest_session(*, hijack_id: str = "h", owner: str = "o", expires_in: float) -> HijackSession:
    now = time.monotonic()
    return HijackSession(
        hijack_id=hijack_id,
        owner=owner,
        acquired_at=now,
        lease_expires_at=now + expires_in,
        last_heartbeat=now,
    )


def _install_swap(registry: WorkerRegistry, original: WorkerTermState) -> WorkerTermState:
    """Register *original* under ``w1`` with a fence that replaces it on entry."""
    replacement = _make_state()
    original.owned_input_fence = _ReplacingFence(registry, "w1", replacement)  # type: ignore[assignment]
    registry.put("w1", original)
    return replacement


# =====================================================================
# ownership_generation += 1
# =====================================================================


class TestOwnershipGenerationIncrements:
    """Every ownership transition must bump the generation by exactly one.

    The generation is the optimistic-concurrency token: ``send_owned_worker``
    and ``run_owned_browser_operation`` refuse to send when the caller's
    captured generation no longer matches. A mutated increment that lands on a
    *reachable* value (``= 1``, ``-= 1``) lets a stale owner's send be accepted
    after ownership has already changed hands.
    """

    async def test_expire_leases_under_lock_bumps_generation_once(self) -> None:
        mgr, registry, _hub = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(expires_in=-1)
        registry.put("w1", st)

        await mgr._expire_leases_under_lock("w1", time.monotonic())

        assert st.ownership_generation == _NEXT_GENERATION

    async def test_release_rest_bumps_generation_once(self) -> None:
        mgr, registry, _hub = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(hijack_id="hid", expires_in=60)
        registry.put("w1", st)

        assert await mgr.release_rest("w1", "hid") == (True, True)
        assert st.ownership_generation == _NEXT_GENERATION

    async def test_try_release_ws_bumps_generation_once(self) -> None:
        mgr, registry, _hub = _make_manager()
        st = _make_state()
        ws = AsyncMock()
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 60
        registry.put("w1", st)

        assert await mgr.try_release_ws("w1", ws) == (True, False)
        assert st.ownership_generation == _NEXT_GENERATION

    async def test_remove_dead_browsers_bumps_generation_once(self) -> None:
        mgr, registry, _hub = _make_manager()
        st = _make_state()
        ws = AsyncMock()
        st.browsers[ws] = "viewer"
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 60
        registry.put("w1", st)

        assert await mgr.remove_dead_browsers("w1", {ws}) is True
        assert st.ownership_generation == _NEXT_GENERATION

    async def test_try_acquire_ws_bumps_generation_once(self) -> None:
        mgr, registry, _hub = _make_manager()
        st = _make_state()
        registry.put("w1", st)

        assert await mgr.try_acquire_ws("w1", AsyncMock()) == (True, None)
        assert st.ownership_generation == _NEXT_GENERATION

    async def test_try_acquire_rest_bumps_generation_once_on_success(self) -> None:
        mgr, registry, _hub = _make_manager()
        st = _make_state()
        registry.put("w1", st)

        result = await mgr.try_acquire_rest("w1", owner="op", lease_s=30, hijack_id="hid", now=1000.0)

        assert result == (True, None)
        assert st.ownership_generation == _NEXT_GENERATION

    async def test_try_acquire_rest_bumps_generation_when_pause_send_fails(self) -> None:
        """A dropped worker socket is itself an ownership transition."""
        mgr, registry, _hub = _make_manager()
        st = _make_state()
        st.worker_ws.send_text.side_effect = RuntimeError("socket gone")  # type: ignore[union-attr]
        registry.put("w1", st)

        result = await mgr.try_acquire_rest("w1", owner="op", lease_s=30, hijack_id="hid", now=1000.0)

        assert result == (False, "no_worker")
        assert st.worker_ws is None
        assert st.ownership_generation == _NEXT_GENERATION


# =====================================================================
# st is not state  (post-fence identity recheck)
# =====================================================================


class TestFenceIdentityRecheck:
    """A registry entry replaced during the fence wait must abort the operation.

    Without the recheck the manager would write a lease onto the *stale* state
    object — which is no longer the one the hub serves — silently losing the
    hijack, or worse, mutating a state another connection now owns.
    """

    async def test_try_acquire_rest_reports_no_worker_when_state_replaced(self) -> None:
        mgr, registry, _hub = _make_manager()
        original = _make_state()
        replacement = _install_swap(registry, original)

        result = await mgr.try_acquire_rest("w1", owner="op", lease_s=30, hijack_id="hid", now=1000.0)

        assert result == (False, "no_worker")
        # Neither state may be touched: the stale one is abandoned, and the
        # replacement was never validated under the lock.
        assert original.hijack_session is None
        assert replacement.hijack_session is None
        assert original.ownership_generation == _SEED_GENERATION
        assert replacement.ownership_generation == _SEED_GENERATION
        # The abort must happen BEFORE the worker is paused. The return value
        # alone cannot show this: with the recheck's ``or`` mutated to ``and``
        # the reservation proceeds, pauses the freshly-reconnected worker, and
        # only then fails the phase-3 identity check — same tuple, but a live
        # worker left paused by a hijack that was never granted.
        assert replacement.worker_ws.send_text.await_count == 0  # type: ignore[union-attr]
        assert replacement.hijack_pending is None

    async def test_try_acquire_ws_reports_no_worker_when_state_replaced(self) -> None:
        mgr, registry, _hub = _make_manager()
        original = _make_state()
        replacement = _install_swap(registry, original)

        result = await mgr.try_acquire_ws("w1", AsyncMock())

        assert result == (False, "no_worker")
        assert original.hijack_owner is None
        assert replacement.hijack_owner is None
        assert original.ownership_generation == _SEED_GENERATION
        assert replacement.ownership_generation == _SEED_GENERATION

    async def test_remove_dead_browsers_returns_false_when_state_replaced(self) -> None:
        mgr, registry, _hub = _make_manager()
        original = _make_state()
        ws = AsyncMock()
        original.browsers[ws] = "viewer"
        original.hijack_owner = ws
        original.hijack_owner_expires_at = time.monotonic() + 60
        replacement = _install_swap(registry, original)

        assert await mgr.remove_dead_browsers("w1", {ws}) is False
        # The stale state keeps its (now irrelevant) browser entry rather than
        # being mutated, and no resume frame is emitted for the live worker.
        assert ws in original.browsers
        assert replacement.ownership_generation == _SEED_GENERATION


# =====================================================================
# Bounded worker I/O
# =====================================================================


class _WaitForSpy:
    """Record the ``timeout`` of every ``asyncio.wait_for`` the module makes."""

    def __init__(self) -> None:
        self.timeouts: list[float | None] = []
        self._real = asyncio.wait_for

    async def __call__(self, awaitable: Any, timeout: float | None) -> Any:
        self.timeouts.append(timeout)
        return await self._real(awaitable, timeout)


class TestBoundedSendTimeout:
    """Worker I/O under the fence must stay bounded.

    The fence serialises input ownership per worker, so an unbounded send holds
    it indefinitely: one backpressured worker would block every acquire,
    release, and expiry sweep for that session.
    """

    async def test_acquire_rest_pause_is_sent_with_the_bounded_timeout(self) -> None:
        mgr, registry, _hub = _make_manager()
        registry.put("w1", _make_state())
        spy = _WaitForSpy()

        with patch("provide.uterm.server.bridge.hub.lease.asyncio.wait_for", spy):
            result = await mgr.try_acquire_rest("w1", owner="op", lease_s=30, hijack_id="hid", now=1000.0)

        assert result == (True, None)
        assert spy.timeouts == [5.0]

    async def test_send_worker_if_unowned_uses_the_bounded_timeout(self) -> None:
        mgr, registry, _hub = _make_manager()
        registry.put("w1", _make_state())
        spy = _WaitForSpy()

        with patch("provide.uterm.server.bridge.hub.lease.asyncio.wait_for", spy):
            assert await mgr.send_worker_if_unowned("w1", {"type": "control"}) is True

        assert spy.timeouts == [5.0]

    async def test_send_worker_if_unowned_returns_false_on_timeout(self) -> None:
        """A timed-out resume must report failure, not a phantom success.

        ``_recheck_and_resume`` only fires ``notify_hijack_changed`` when this
        returns True; a mutated ``return True`` would announce the worker as
        un-hijacked while it is still paused.
        """
        mgr, registry, hub = _make_manager()
        registry.put("w1", _make_state())
        hub.send_worker_exc = TimeoutError()

        assert await mgr.send_worker_if_unowned("w1", {"type": "control"}) is False
