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

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from provide.uterm.server.bridge.frames import make_worker_disconnected_frame
from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState


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
