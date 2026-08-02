#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing unit tests for :class:`ConnectionManager` hijack-clearing lifecycle.

Targets the two hijack-clearing-lifecycle methods of
``provide.uterm.server.bridge.hub.connection.ConnectionManager`` that the
mutation gate reported as untested:

* :meth:`ConnectionManager.disconnect_worker` (48 surviving mutants)
* :meth:`ConnectionManager._event_bus_close` (3 surviving mutants)

Every test constructs a FRESH :class:`TermHub`, sets worker state via the
hub lock, and pins every observable: the exact bool return, every
``WorkerTermState`` field mutated/cleared (``worker_ws``,
``hijack_session``, ``hijack_owner``, ``hijack_owner_expires_at``), the
``ws.close`` await, the exact broadcast payload, the exact
``notify_hijack_changed`` kwargs, and which hub callbacks
(``broadcast`` / ``broadcast_hijack_state`` / ``prune_if_idle`` /
``_event_bus_close``) fired and in what order. ``logger.debug`` on a close
error is asserted via a patched module-level logger (the hijack-clearing
bodies now live in ``connection_hijack``, so the patch targets
``connection_hijack.logger``).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from provide.uterm.server.bridge.frames import make_worker_disconnected_frame
from provide.uterm.server.bridge.hub import TermHub, connection_hijack
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState


def _rest_session(owner: str) -> HijackSession:
    now = time.monotonic()
    return HijackSession(
        hijack_id=f"hid-{owner}",
        owner=owner,
        acquired_at=now,
        lease_expires_at=now + 60,
        last_heartbeat=now,
    )


async def _await_fence_contention(fence: asyncio.Lock) -> None:
    """Yield until the call under test is parked on *fence*."""
    for _ in range(200):
        if getattr(fence, "_waiters", None):
            return
        await asyncio.sleep(0)
    raise AssertionError("the call never reached the owned-input fence")


def _install_recorders(hub: TermHub) -> dict[str, Any]:
    """Replace the four hub callbacks invoked by ``disconnect_worker`` with recorders.

    Returns a dict of mocks keyed by callback name plus a shared ``order``
    list so tests can assert both the arguments and the relative ordering
    of the delegated side effects.
    """
    order: list[str] = []

    async def _broadcast(worker_id: str, msg: dict[str, Any]) -> None:
        order.append("broadcast")
        recorders["broadcast"](worker_id, msg)

    def _notify(worker_id: str, *, enabled: bool, owner: str | None = None) -> None:
        order.append("notify")
        recorders["notify"](worker_id, enabled=enabled, owner=owner)

    async def _broadcast_hijack_state(worker_id: str) -> None:
        order.append("broadcast_hijack_state")
        recorders["broadcast_hijack_state"](worker_id)

    async def _prune(worker_id: str) -> None:
        order.append("prune")
        recorders["prune"](worker_id)

    recorders: dict[str, Any] = {
        "broadcast": MagicMock(),
        "notify": MagicMock(),
        "broadcast_hijack_state": MagicMock(),
        "prune": MagicMock(),
        "order": order,
    }
    hub.broadcast = _broadcast  # type: ignore[method-assign]
    hub.notify_hijack_changed = _notify  # type: ignore[method-assign]
    hub.broadcast_hijack_state = _broadcast_hijack_state  # type: ignore[method-assign]
    hub.prune_if_idle = _prune  # type: ignore[method-assign]
    return recorders


# ---------------------------------------------------------------------------
# disconnect_worker — early-exit (returns False, no side effects)
# ---------------------------------------------------------------------------


class TestDisconnectWorkerEarlyExit:
    async def test_returns_false_when_worker_unknown(self) -> None:
        """st is None → returns False, no callback fires, no state created."""
        hub = TermHub()
        rec = _install_recorders(hub)

        result = await hub.disconnect_worker("ghost")

        assert result is False
        rec["broadcast"].assert_not_called()
        rec["notify"].assert_not_called()
        rec["broadcast_hijack_state"].assert_not_called()
        rec["prune"].assert_not_called()
        assert rec["order"] == []
        # No worker state was created as a side effect.
        assert "ghost" not in hub.registry._workers

    async def test_returns_false_when_worker_ws_none(self) -> None:
        """st present but worker_ws is None → returns False, no callbacks, state untouched."""
        hub = TermHub()
        rec = _install_recorders(hub)
        worker_id = "w-no-ws"
        now = time.monotonic()
        async with hub._lock:
            st = WorkerTermState()
            st.worker_ws = None
            # Hijack fields present to prove the no-ws guard returns BEFORE clearing them.
            st.hijack_session = HijackSession(
                hijack_id="hid",
                owner="alice",
                acquired_at=now,
                lease_expires_at=now + 60,
                last_heartbeat=now,
            )
            st.hijack_owner = MagicMock()
            st.hijack_owner_expires_at = now + 60
            hub.registry._workers[worker_id] = st

        result = await hub.disconnect_worker(worker_id)

        assert result is False
        rec["broadcast"].assert_not_called()
        rec["notify"].assert_not_called()
        rec["broadcast_hijack_state"].assert_not_called()
        rec["prune"].assert_not_called()
        # Hijack state must remain intact (early return, no clearing).
        st_after = hub.registry._workers[worker_id]
        assert st_after.hijack_session is not None
        assert st_after.hijack_owner is not None
        assert st_after.hijack_owner_expires_at == now + 60
        assert st_after.worker_ws is None


# ---------------------------------------------------------------------------
# disconnect_worker — happy paths (clears state, broadcasts, prunes)
# ---------------------------------------------------------------------------


class TestDisconnectWorkerClears:
    async def test_not_hijacked_clears_ws_only_no_hijack_broadcast(self) -> None:
        """worker_ws set, no hijack → clears worker_ws, broadcasts disconnect, prunes, returns True.

        was_hijacked is False so notify_hijack_changed and broadcast_hijack_state
        must NOT fire. Event bus is None so _event_bus_close is not reached.
        """
        hub = TermHub()
        rec = _install_recorders(hub)
        worker_id = "w-plain"
        ws = MagicMock()
        ws.close = AsyncMock()
        async with hub._lock:
            st = WorkerTermState()
            st.worker_ws = ws
            hub.registry._workers[worker_id] = st

        result = await hub.disconnect_worker(worker_id)

        assert result is True
        # worker_ws cleared, hijack fields stay None.
        st_after = hub.registry._workers[worker_id]
        assert st_after.worker_ws is None
        assert st_after.hijack_session is None
        assert st_after.hijack_owner is None
        assert st_after.hijack_owner_expires_at is None
        # ws.close awaited exactly once.
        ws.close.assert_awaited_once_with()
        # Disconnect frame broadcast with the exact payload.
        assert rec["broadcast"].call_count == 1
        b_args, _ = rec["broadcast"].call_args
        assert b_args[0] == worker_id
        expected_frame = make_worker_disconnected_frame(worker_id)
        assert b_args[1]["type"] == expected_frame["type"] == "worker_disconnected"
        assert b_args[1]["worker_id"] == worker_id
        # No hijack-clearing notification path.
        rec["notify"].assert_not_called()
        rec["broadcast_hijack_state"].assert_not_called()
        # prune_if_idle invoked exactly once with the worker id.
        rec["prune"].assert_called_once_with(worker_id)
        # Ordering: broadcast (disconnect) before prune; no hijack steps between.
        assert rec["order"] == ["broadcast", "prune"]

    async def test_rest_hijacked_fires_hijack_cleared_notification(self) -> None:
        """worker_ws + REST hijack_session → was_hijacked True: notify + broadcast_hijack_state fire.

        Pins the exact notify_hijack_changed kwargs (enabled=False, owner=None)
        and the full ordering of the four delegated side effects.
        """
        hub = TermHub()
        rec = _install_recorders(hub)
        worker_id = "w-rest-hj"
        ws = MagicMock()
        ws.close = AsyncMock()
        now = time.monotonic()
        async with hub._lock:
            st = WorkerTermState()
            st.worker_ws = ws
            st.hijack_session = HijackSession(
                hijack_id="hid",
                owner="alice",
                acquired_at=now,
                lease_expires_at=now + 60,
                last_heartbeat=now,
            )
            hub.registry._workers[worker_id] = st

        result = await hub.disconnect_worker(worker_id)

        assert result is True
        st_after = hub.registry._workers[worker_id]
        assert st_after.worker_ws is None
        assert st_after.hijack_session is None
        assert st_after.hijack_owner is None
        assert st_after.hijack_owner_expires_at is None
        ws.close.assert_awaited_once_with()
        # Hijack-cleared notification with the exact kwargs.
        rec["notify"].assert_called_once_with(worker_id, enabled=False, owner=None)
        rec["broadcast_hijack_state"].assert_called_once_with(worker_id)
        rec["broadcast"].assert_called_once()
        rec["prune"].assert_called_once_with(worker_id)
        # Exact ordering: disconnect broadcast → notify → hijack-state broadcast → prune.
        assert rec["order"] == ["broadcast", "notify", "broadcast_hijack_state", "prune"]

    async def test_dashboard_owner_hijack_fires_hijack_cleared_notification(self) -> None:
        """worker_ws + dashboard hijack_owner (no session) → was_hijacked True via owner branch."""
        hub = TermHub()
        rec = _install_recorders(hub)
        worker_id = "w-dash-hj"
        ws = MagicMock()
        ws.close = AsyncMock()
        owner_ws = MagicMock()
        now = time.monotonic()
        async with hub._lock:
            st = WorkerTermState()
            st.worker_ws = ws
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = now + 60
            hub.registry._workers[worker_id] = st

        result = await hub.disconnect_worker(worker_id)

        assert result is True
        st_after = hub.registry._workers[worker_id]
        assert st_after.worker_ws is None
        assert st_after.hijack_owner is None
        assert st_after.hijack_owner_expires_at is None
        rec["notify"].assert_called_once_with(worker_id, enabled=False, owner=None)
        rec["broadcast_hijack_state"].assert_called_once_with(worker_id)
        assert rec["order"] == ["broadcast", "notify", "broadcast_hijack_state", "prune"]


# ---------------------------------------------------------------------------
# disconnect_worker — ws.close error → logger.debug, flow continues
# ---------------------------------------------------------------------------


class TestDisconnectWorkerCloseError:
    async def test_close_error_logs_debug_and_continues(self) -> None:
        """ws.close raises → logger.debug fires with exact format, broadcast+prune still run, returns True."""
        hub = TermHub()
        rec = _install_recorders(hub)
        worker_id = "w-close-err"
        ws = MagicMock()
        boom = RuntimeError("socket gone")
        ws.close = AsyncMock(side_effect=boom)
        async with hub._lock:
            st = WorkerTermState()
            st.worker_ws = ws
            hub.registry._workers[worker_id] = st

        with patch("provide.uterm.server.bridge.hub.connection_hijack.logger") as mock_logger:
            result = await hub.disconnect_worker(worker_id)

        assert result is True
        ws.close.assert_awaited_once_with()
        # The except branch logs at DEBUG with the exact %-format string + positional args.
        mock_logger.debug.assert_called_once_with("disconnect_worker close error worker_id=%s: %s", worker_id, boom)
        # Flow continues past the swallowed close error.
        rec["broadcast"].assert_called_once()
        rec["prune"].assert_called_once_with(worker_id)
        assert rec["order"] == ["broadcast", "prune"]

    async def test_no_close_error_does_not_log_debug(self) -> None:
        """ws.close succeeds → logger.debug must NOT fire (distinct outcome from the error branch)."""
        hub = TermHub()
        _install_recorders(hub)
        worker_id = "w-close-ok"
        ws = MagicMock()
        ws.close = AsyncMock()
        async with hub._lock:
            st = WorkerTermState()
            st.worker_ws = ws
            hub.registry._workers[worker_id] = st

        with patch("provide.uterm.server.bridge.hub.connection_hijack.logger") as mock_logger:
            result = await hub.disconnect_worker(worker_id)

        assert result is True
        mock_logger.debug.assert_not_called()


# ---------------------------------------------------------------------------
# disconnect_worker — event-bus close delegation
# ---------------------------------------------------------------------------


class TestDisconnectWorkerEventBus:
    async def test_event_bus_set_invokes_event_bus_close(self) -> None:
        """_event_bus is not None → _event_bus_close(worker_id) is invoked once."""
        hub = TermHub()
        _install_recorders(hub)
        worker_id = "w-bus"
        ws = MagicMock()
        ws.close = AsyncMock()
        async with hub._lock:
            st = WorkerTermState()
            st.worker_ws = ws
            hub.registry._workers[worker_id] = st

        bus = MagicMock()
        hub._event_bus = bus

        # Patch the class method so we can assert the guarded call fired exactly
        # once with the worker id (ConnectionManager uses __slots__, so the
        # method cannot be patched on the instance).
        from provide.uterm.server.bridge.hub.connection import ConnectionManager

        with patch.object(ConnectionManager, "_event_bus_close", autospec=True) as spy:
            result = await hub.disconnect_worker(worker_id)

        assert result is True
        spy.assert_called_once_with(hub.connection_mgr, worker_id)

    async def test_event_bus_none_skips_event_bus_close(self) -> None:
        """_event_bus is None → _event_bus_close is NOT invoked (distinct branch outcome)."""
        hub = TermHub()
        _install_recorders(hub)
        worker_id = "w-no-bus"
        ws = MagicMock()
        ws.close = AsyncMock()
        async with hub._lock:
            st = WorkerTermState()
            st.worker_ws = ws
            hub.registry._workers[worker_id] = st

        assert hub._event_bus is None

        from provide.uterm.server.bridge.hub.connection import ConnectionManager

        with patch.object(ConnectionManager, "_event_bus_close", autospec=True) as spy:
            result = await hub.disconnect_worker(worker_id)

        assert result is True
        spy.assert_not_called()


# ---------------------------------------------------------------------------
# _event_bus_close — direct unit tests
# ---------------------------------------------------------------------------


class TestEventBusClose:
    def test_calls_close_worker_when_bus_set(self) -> None:
        """bus present → bus.close_worker(worker_id) called exactly once with the worker id."""
        hub = TermHub()
        bus = MagicMock()
        hub._event_bus = bus

        hub.connection_mgr._event_bus_close("w-direct")

        bus.close_worker.assert_called_once_with("w-direct")

    def test_noop_when_bus_none(self) -> None:
        """bus is None → method is a no-op (no attribute access on None, no raise)."""
        hub = TermHub()
        assert hub._event_bus is None

        # Must not raise even though there is no bus to call.
        hub.connection_mgr._event_bus_close("w-direct")

    def test_closes_the_private_operation_stream_for_the_same_worker(self) -> None:
        """Supervised operations read a SECOND, private bus — close that too.

        The operation bus carries unredacted terminal output and is never
        exposed by a route, so nothing else ever ends its streams. Closing only
        the public bus would leave an in-flight operation capture parked on its
        queue until its own hard cap, long after the session it was watching
        went away.
        """
        hub = TermHub()
        public = MagicMock()
        private = MagicMock()
        hub._event_bus = public
        hub._operation_event_bus = private

        hub.connection_mgr._event_bus_close("w-both")

        public.close_worker.assert_called_once_with("w-both")
        private.close_worker.assert_called_once_with("w-both")

    def test_public_bus_closes_even_with_no_private_operation_bus(self) -> None:
        """Private bus absent → still a no-op for it, and the public close stands."""
        hub = TermHub()
        public = MagicMock()
        hub._event_bus = public
        assert hub._operation_event_bus is None

        hub.connection_mgr._event_bus_close("w-public-only")

        public.close_worker.assert_called_once_with("w-public-only")


# ---------------------------------------------------------------------------
# disconnect_worker / force_release_hijack — owned-input fence re-check
# ---------------------------------------------------------------------------


class TestOwnedInputFenceRecheck:
    """Both bodies capture the state under the hub lock, then take its fence.

    The hub lock is released for that wait, so a reconnect can replace the whole
    ``WorkerTermState`` in the window. The second in-lock check exists to notice
    that and abandon the operation; without it the call would clear a session it
    never inspected.
    """

    async def test_disconnect_worker_refuses_a_state_replaced_behind_the_fence(self) -> None:
        hub = TermHub()
        rec = _install_recorders(hub)
        worker_id = "w-fence-replaced"
        old_ws = MagicMock()
        old_ws.close = AsyncMock()
        new_ws = MagicMock()
        new_ws.close = AsyncMock()
        async with hub._lock:
            original = WorkerTermState()
            original.worker_ws = old_ws
            hub.registry._workers[worker_id] = original

        await original.owned_input_fence.acquire()
        pending = asyncio.create_task(hub.disconnect_worker(worker_id))
        await _await_fence_contention(original.owned_input_fence)

        replacement = WorkerTermState()
        replacement.worker_ws = new_ws
        hub.registry._workers[worker_id] = replacement
        original.owned_input_fence.release()

        assert await asyncio.wait_for(pending, timeout=1.0) is False
        # The replacement's live socket must survive untouched.
        assert replacement.worker_ws is new_ws
        new_ws.close.assert_not_awaited()
        old_ws.close.assert_not_awaited()
        rec["broadcast"].assert_not_called()
        rec["prune"].assert_not_called()
        assert rec["order"] == []

    async def test_force_release_refuses_a_state_replaced_behind_the_fence(self) -> None:
        hub = TermHub()
        rec = _install_recorders(hub)
        send_worker = AsyncMock(return_value=True)
        hub.send_worker = send_worker  # type: ignore[method-assign]
        worker_id = "w-force-replaced"
        async with hub._lock:
            original = WorkerTermState()
            original.hijack_session = _rest_session("alice")
            hub.registry._workers[worker_id] = original

        await original.owned_input_fence.acquire()
        pending = asyncio.create_task(hub.connection_mgr.force_release_hijack(worker_id))
        await _await_fence_contention(original.owned_input_fence)

        replacement = WorkerTermState()
        replacement.hijack_session = _rest_session("bob")
        hub.registry._workers[worker_id] = replacement
        original.owned_input_fence.release()

        assert await asyncio.wait_for(pending, timeout=1.0) is False
        # The replacement's freshly acquired lease must not be torn down.
        assert replacement.hijack_session is not None
        assert replacement.hijack_session.owner == "bob"
        send_worker.assert_not_awaited()
        rec["notify"].assert_not_called()
        rec["broadcast_hijack_state"].assert_not_called()


# ---------------------------------------------------------------------------
# Ownership-epoch arithmetic
# ---------------------------------------------------------------------------


class TestOwnershipGenerationBump:
    """``ownership_generation`` is the token a held approval is matched against.

    Approvals capture the epoch they were granted in, so the step size is
    load-bearing: a value that resets, decrements, or skips lets a captured
    approval from an earlier epoch compare equal again and revive stale input.
    """

    async def test_disconnect_worker_advances_the_epoch_by_exactly_one(self) -> None:
        hub = TermHub()
        _install_recorders(hub)
        worker_id = "w-gen-disconnect"
        ws = MagicMock()
        ws.close = AsyncMock()
        async with hub._lock:
            st = WorkerTermState()
            st.worker_ws = ws
            st.ownership_generation = 7
            hub.registry._workers[worker_id] = st

        assert await hub.disconnect_worker(worker_id) is True

        assert hub.registry._workers[worker_id].ownership_generation == 8

    async def test_force_release_advances_the_epoch_by_exactly_one(self) -> None:
        hub = TermHub()
        _install_recorders(hub)
        hub.send_worker = AsyncMock(return_value=True)  # type: ignore[method-assign]
        worker_id = "w-gen-force"
        async with hub._lock:
            st = WorkerTermState()
            st.hijack_session = _rest_session("carol")
            st.ownership_generation = 3
            hub.registry._workers[worker_id] = st

        assert await hub.connection_mgr.force_release_hijack(worker_id) is True

        assert hub.registry._workers[worker_id].ownership_generation == 4

    async def test_force_release_leaves_the_epoch_alone_when_no_hijack_was_held(self) -> None:
        """Nothing was revoked, so no approval needs invalidating."""
        hub = TermHub()
        _install_recorders(hub)
        worker_id = "w-gen-noop"
        async with hub._lock:
            st = WorkerTermState()
            st.ownership_generation = 5
            hub.registry._workers[worker_id] = st

        assert await hub.connection_mgr.force_release_hijack(worker_id) is False

        assert hub.registry._workers[worker_id].ownership_generation == 5


# ---------------------------------------------------------------------------
# force_release_hijack — bounded resume send
# ---------------------------------------------------------------------------


class TestForceReleaseResumeSendBudget:
    """The resume frame is sent while the worker's ownership fence is held.

    An unbounded send would therefore wedge every later lease transition for
    that worker behind one unresponsive socket, so the wait is capped — and the
    cap is asserted by value, not merely by presence.
    """

    async def test_resume_send_is_bounded_by_a_five_second_budget(self) -> None:
        hub = TermHub()
        _install_recorders(hub)
        hub.send_worker = AsyncMock(return_value=True)  # type: ignore[method-assign]
        worker_id = "w-resume-budget"
        async with hub._lock:
            st = WorkerTermState()
            st.hijack_session = _rest_session("dana")
            hub.registry._workers[worker_id] = st

        budgets: list[Any] = []

        async def _wait_for(awaitable: Any, timeout: Any = None) -> Any:
            budgets.append(timeout)
            return await awaitable

        with patch.object(connection_hijack, "asyncio", SimpleNamespace(wait_for=_wait_for)):
            assert await hub.connection_mgr.force_release_hijack(worker_id) is True

        assert budgets == [5.0]

    async def test_resume_timeout_is_logged_with_the_worker_and_release_completes(self) -> None:
        """A worker that never acks must not hold the release hostage.

        The lease is already cleared under the lock by this point, so the only
        correct outcome is: log which worker went silent, then finish notifying
        and re-broadcasting exactly as a successful send would.
        """
        hub = TermHub()
        rec = _install_recorders(hub)
        hub.send_worker = AsyncMock(return_value=True)  # type: ignore[method-assign]
        worker_id = "w-resume-timeout"
        async with hub._lock:
            st = WorkerTermState()
            st.hijack_session = _rest_session("erin")
            hub.registry._workers[worker_id] = st

        async def _wait_for(awaitable: Any, timeout: Any = None) -> Any:
            del timeout
            awaitable.close()
            raise TimeoutError

        with (
            patch.object(connection_hijack, "asyncio", SimpleNamespace(wait_for=_wait_for)),
            patch.object(connection_hijack, "logger") as mock_logger,
        ):
            assert await hub.connection_mgr.force_release_hijack(worker_id) is True

        mock_logger.warning.assert_called_once_with("force_release_resume_timeout worker_id=%s", worker_id)
        assert hub.registry._workers[worker_id].hijack_session is None
        rec["notify"].assert_called_once_with(worker_id, enabled=False, owner=None)
        rec["broadcast_hijack_state"].assert_called_once_with(worker_id)

    async def test_successful_resume_send_logs_no_timeout_warning(self) -> None:
        """Distinct outcome from the timeout branch: nothing is logged."""
        hub = TermHub()
        _install_recorders(hub)
        hub.send_worker = AsyncMock(return_value=True)  # type: ignore[method-assign]
        worker_id = "w-resume-ok"
        async with hub._lock:
            st = WorkerTermState()
            st.hijack_session = _rest_session("frank")
            hub.registry._workers[worker_id] = st

        with patch.object(connection_hijack, "logger") as mock_logger:
            assert await hub.connection_mgr.force_release_hijack(worker_id) is True

        mock_logger.warning.assert_not_called()
