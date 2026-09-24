#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing unit tests for the module-level hub delegate functions in
:mod:`provide.uterm.server.bridge.hub.core_delegates_lease` and
:mod:`provide.uterm.server.bridge.hub.core_delegates_connection`.

Each delegate takes a ``hub`` first argument and forwards to a service
(``hub.lease`` / ``hub.connection_mgr`` / ``hub.router``) plus, on the
success branch, calls ``hub.emit_telemetry(...)``. Rather than exercising
these through a real :class:`TermHub`, every test here builds a minimal
fake hub (a :class:`~unittest.mock.MagicMock` with the exact
attributes/services each delegate touches) so every forwarded argument,
return value, and telemetry call can be pinned exactly -- without ever
monkeypatching the delegate functions under test.

Telemetry assertions compare ``hub.emit_telemetry.await_args_list`` against
an exact list of :func:`unittest.mock.call` objects. That single equality
kills every mutant that renames an event string, swaps a metadata key or
value, changes case, passes ``None`` for an argument, or drops a keyword
argument outright -- any one of those changes produces a different
``await_args_list``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

from provide.uterm.server.bridge.hub import core_delegates_connection as cdc
from provide.uterm.server.bridge.hub import core_delegates_lease as cdl

# A fixed "now" used to pin the <= vs < lease/owner-expiry boundaries exactly.
FAKE_NOW = 1_000_000.0

# String target for monkeypatch.setattr: core_delegates_lease does `import time`
# (not `from time import monotonic`), and mypy strict (--no-implicit-reexport)
# flags `cdl.time` as attr-defined since "time" isn't in the module's __all__.
# A string target sidesteps that without weakening the runtime patch.
_TIME_MONOTONIC = "provide.uterm.server.bridge.hub.core_delegates_lease.time.monotonic"


def _make_hub(**overrides: Any) -> MagicMock:
    """A fake TermHub exposing exactly the attributes the delegates touch."""
    hub = MagicMock(name="hub")

    hub.registry = MagicMock(name="registry")
    hub.registry._workers = {}

    hub.lease = MagicMock(name="lease")
    hub.lease.cleanup_expired = AsyncMock(return_value=False)
    hub.lease._get_rest_session_no_cleanup = AsyncMock(return_value=None)
    hub.lease.try_acquire_rest = AsyncMock(return_value=(True, None))
    hub.lease.try_acquire_ws = AsyncMock(return_value=(True, None))
    hub.lease.try_release_ws = AsyncMock(return_value=(True, False))
    hub.lease.remove_dead_browsers = AsyncMock(return_value=True)
    hub.lease.dashboard_hijack_lease_s = 999

    hub.connection_mgr = MagicMock(name="connection_mgr")
    hub.connection_mgr.register_worker = AsyncMock(return_value=False)
    hub.connection_mgr.register_browser = AsyncMock(return_value={})
    hub.connection_mgr.cleanup_browser_disconnect = AsyncMock(return_value={})
    hub.connection_mgr.deregister_worker = AsyncMock(return_value=(False, False))

    hub.router = MagicMock(name="router")

    hub.emit_telemetry = AsyncMock()
    hub.cleanup_expired_hijack = AsyncMock(return_value=False)

    hub._input_buffers = {}
    hub._hold_buffers = {}
    hub._paused_browsers = set()
    hub._startup_pending_browsers = set()
    hub._startup_pending_frames = {}
    hub._event_bus = None
    hub._operation_event_bus = None

    for key, value in overrides.items():
        setattr(hub, key, value)
    return hub


async def _await(coro: Any) -> Any:
    return await asyncio.wait_for(coro, 2)


# ===========================================================================
# cleanup_expired_hijack
# ===========================================================================


class TestCleanupExpiredHijack:
    async def test_rest_boundary_expired_emits_exact_rest_telemetry(self, monkeypatch: Any) -> None:
        """lease_expires_at == now counts as expired (<=), st is looked up by worker_id."""
        monkeypatch.setattr(_TIME_MONOTONIC, lambda: FAKE_NOW)
        hub = _make_hub()
        st = SimpleNamespace(
            hijack_session=SimpleNamespace(lease_expires_at=FAKE_NOW),
            hijack_owner=None,
            hijack_owner_expires_at=None,
        )
        hub.registry._workers = {"w1": st}
        hub.lease.cleanup_expired = AsyncMock(return_value=True)

        result = await _await(cdl.cleanup_expired_hijack(hub, "w1"))

        assert result is True
        hub.lease.cleanup_expired.assert_awaited_once_with("w1")
        assert hub.emit_telemetry.await_args_list == [
            call("hijack.expired", worker_id="w1", metadata={"hijack_type": "rest"}),
        ]

    async def test_dashboard_boundary_expired_emits_exact_dashboard_telemetry(self, monkeypatch: Any) -> None:
        """hijack_owner_expires_at == now counts as expired (<=); no rest session present."""
        monkeypatch.setattr(_TIME_MONOTONIC, lambda: FAKE_NOW)
        hub = _make_hub()
        owner = object()
        st = SimpleNamespace(hijack_session=None, hijack_owner=owner, hijack_owner_expires_at=FAKE_NOW)
        hub.registry._workers = {"w2": st}
        hub.lease.cleanup_expired = AsyncMock(return_value=True)

        result = await _await(cdl.cleanup_expired_hijack(hub, "w2"))

        assert result is True
        assert hub.emit_telemetry.await_args_list == [
            call("hijack.expired", worker_id="w2", metadata={"hijack_type": "dashboard"}),
        ]

    async def test_not_cleaned_emits_no_telemetry(self, monkeypatch: Any) -> None:
        """cleanup_expired() returning False suppresses both telemetry emissions."""
        monkeypatch.setattr(_TIME_MONOTONIC, lambda: FAKE_NOW)
        hub = _make_hub()
        st = SimpleNamespace(
            hijack_session=SimpleNamespace(lease_expires_at=FAKE_NOW - 1.0),
            hijack_owner=object(),
            hijack_owner_expires_at=FAKE_NOW - 1.0,
        )
        hub.registry._workers = {"w3": st}
        hub.lease.cleanup_expired = AsyncMock(return_value=False)

        result = await _await(cdl.cleanup_expired_hijack(hub, "w3"))

        assert result is False
        hub.emit_telemetry.assert_not_awaited()


# ===========================================================================
# get_rest_session
# ===========================================================================


class TestGetRestSession:
    async def test_cleans_up_then_looks_up_by_worker_id(self) -> None:
        hub = _make_hub()
        sentinel_session = object()
        hub.lease._get_rest_session_no_cleanup = AsyncMock(return_value=sentinel_session)

        result = await _await(cdl.get_rest_session(hub, "w1", "hid1"))

        hub.cleanup_expired_hijack.assert_awaited_once_with("w1")
        hub.lease._get_rest_session_no_cleanup.assert_awaited_once_with("w1", "hid1")
        assert result is sentinel_session


# ===========================================================================
# try_acquire_rest_hijack
# ===========================================================================


class TestTryAcquireRestHijack:
    async def test_success_forwards_args_and_emits_exact_telemetry(self) -> None:
        hub = _make_hub()
        hub.lease.try_acquire_rest = AsyncMock(return_value=(True, None))

        result = await _await(
            cdl.try_acquire_rest_hijack(hub, "w1", owner="alice", lease_s=30, hijack_id="hid1", now=123.0)
        )

        assert result == (True, None)
        hub.lease.try_acquire_rest.assert_awaited_once_with(
            "w1", owner="alice", lease_s=30, hijack_id="hid1", now=123.0
        )
        assert hub.emit_telemetry.await_args_list == [
            call(
                "hijack.acquired",
                worker_id="w1",
                principal="alice",
                metadata={"hijack_type": "rest", "lease_s": 30},
            ),
        ]

    async def test_failure_emits_no_telemetry(self) -> None:
        hub = _make_hub()
        hub.lease.try_acquire_rest = AsyncMock(return_value=(False, "denied"))

        result = await _await(
            cdl.try_acquire_rest_hijack(hub, "w1", owner="alice", lease_s=30, hijack_id="hid1", now=123.0)
        )

        assert result == (False, "denied")
        hub.emit_telemetry.assert_not_awaited()


# ===========================================================================
# try_acquire_ws_hijack
# ===========================================================================


class TestTryAcquireWsHijack:
    async def test_success_forwards_ws_and_emits_exact_telemetry(self) -> None:
        hub = _make_hub()
        hub.lease.dashboard_hijack_lease_s = 77
        hub.lease.try_acquire_ws = AsyncMock(return_value=(True, None))
        ws = MagicMock()

        result = await _await(cdl.try_acquire_ws_hijack(hub, "w1", ws))

        assert result == (True, None)
        hub.lease.try_acquire_ws.assert_awaited_once_with("w1", ws)
        assert hub.emit_telemetry.await_args_list == [
            call(
                "hijack.acquired",
                worker_id="w1",
                metadata={"hijack_type": "dashboard", "lease_s": 77},
            ),
        ]

    async def test_failure_emits_no_telemetry(self) -> None:
        hub = _make_hub()
        hub.lease.try_acquire_ws = AsyncMock(return_value=(False, "denied"))
        ws = MagicMock()

        result = await _await(cdl.try_acquire_ws_hijack(hub, "w1", ws))

        assert result == (False, "denied")
        hub.emit_telemetry.assert_not_awaited()


# ===========================================================================
# try_release_ws_hijack
# ===========================================================================


class TestTryReleaseWsHijack:
    async def test_success_forwards_ws_and_emits_exact_telemetry(self) -> None:
        hub = _make_hub()
        hub.lease.try_release_ws = AsyncMock(return_value=(True, True))
        ws = MagicMock()

        result = await _await(cdl.try_release_ws_hijack(hub, "w1", ws))

        assert result == (True, True)
        hub.lease.try_release_ws.assert_awaited_once_with("w1", ws)
        assert hub.emit_telemetry.await_args_list == [
            call("hijack.released", worker_id="w1", metadata={"hijack_type": "dashboard"}),
        ]

    async def test_failure_emits_no_telemetry(self) -> None:
        hub = _make_hub()
        hub.lease.try_release_ws = AsyncMock(return_value=(False, False))
        ws = MagicMock()

        result = await _await(cdl.try_release_ws_hijack(hub, "w1", ws))

        assert result == (False, False)
        hub.emit_telemetry.assert_not_awaited()


# ===========================================================================
# register_worker
# ===========================================================================


class TestRegisterWorker:
    async def test_default_is_tunnel_worker_false_and_exact_telemetry(self) -> None:
        hub = _make_hub()
        hub.connection_mgr.register_worker = AsyncMock(return_value=False)
        ws = MagicMock()

        result = await _await(cdc.register_worker(hub, "w1", ws))

        assert result is False
        hub.connection_mgr.register_worker.assert_awaited_once_with("w1", ws, is_tunnel_worker=False)
        assert hub.emit_telemetry.await_args_list == [
            call("session.registered", worker_id="w1", metadata={"session_type": "worker"}),
        ]

    async def test_forwards_is_tunnel_worker_true(self) -> None:
        hub = _make_hub()
        hub.connection_mgr.register_worker = AsyncMock(return_value=True)
        ws = MagicMock()

        result = await _await(cdc.register_worker(hub, "w1", ws, is_tunnel_worker=True))

        assert result is True
        hub.connection_mgr.register_worker.assert_awaited_once_with("w1", ws, is_tunnel_worker=True)


# ===========================================================================
# register_browser
# ===========================================================================


class TestRegisterBrowser:
    async def test_default_defer_broadcast_false_and_exact_telemetry(self) -> None:
        hub = _make_hub()
        hub.connection_mgr.register_browser = AsyncMock(return_value={"state": "ok"})
        ws = MagicMock()

        result = await _await(cdc.register_browser(hub, "w1", ws, "viewer"))

        assert result == {"state": "ok"}
        hub.connection_mgr.register_browser.assert_awaited_once_with("w1", ws, "viewer", defer_broadcast=False)
        assert hub.emit_telemetry.await_args_list == [
            call(
                "session.registered",
                worker_id="w1",
                role="viewer",
                metadata={"session_type": "browser"},
            ),
        ]

    async def test_forwards_defer_broadcast_true(self) -> None:
        hub = _make_hub()
        hub.connection_mgr.register_browser = AsyncMock(return_value={})
        ws = MagicMock()

        await _await(cdc.register_browser(hub, "w1", ws, "operator", defer_broadcast=True))

        hub.connection_mgr.register_browser.assert_awaited_once_with("w1", ws, "operator", defer_broadcast=True)


# ===========================================================================
# cleanup_browser_disconnect
# ===========================================================================


class TestCleanupBrowserDisconnect:
    async def test_clears_buffers_forgets_router_and_emits_exact_telemetry(self) -> None:
        hub = _make_hub()
        ws = MagicMock()
        hub._input_buffers = {ws: "in"}
        hub._hold_buffers = {ws: "hold"}
        hub._paused_browsers = {ws}
        hub.connection_mgr.cleanup_browser_disconnect = AsyncMock(return_value={"ok": True})

        result = await _await(cdc.cleanup_browser_disconnect(hub, "w1", ws, True))

        assert result == {"ok": True}
        hub.router.forget_browser.assert_called_once_with(ws)
        assert ws not in hub._input_buffers
        assert ws not in hub._hold_buffers
        assert ws not in hub._paused_browsers
        hub.connection_mgr.cleanup_browser_disconnect.assert_awaited_once_with("w1", ws, True)
        assert hub.emit_telemetry.await_args_list == [
            call("session.disconnected", worker_id="w1", metadata={"session_type": "browser"}),
        ]


# ===========================================================================
# remove_dead_browsers
# ===========================================================================


class TestRemoveDeadBrowsers:
    async def test_clears_state_for_each_dead_ws_and_forwards_to_lease(self) -> None:
        hub = _make_hub()
        ws = MagicMock()
        dead: Any = {ws}
        hub._hold_buffers = {ws: "hold"}
        hub._startup_pending_browsers = {ws}
        hub._startup_pending_frames = {ws: "frame"}
        hub._paused_browsers = {ws}
        hub.lease.remove_dead_browsers = AsyncMock(return_value=True)

        result = await _await(cdc.remove_dead_browsers(hub, "w1", dead))

        assert result is True
        hub.router.forget_browser.assert_called_once_with(ws)
        assert ws not in hub._hold_buffers
        assert ws not in hub._startup_pending_browsers
        assert ws not in hub._startup_pending_frames
        assert ws not in hub._paused_browsers
        hub.lease.remove_dead_browsers.assert_awaited_once_with("w1", dead)


# ===========================================================================
# deregister_worker
# ===========================================================================


class TestDeregisterWorker:
    async def test_broadcast_closes_both_event_buses_with_worker_id(self) -> None:
        hub = _make_hub()
        hub.connection_mgr.deregister_worker = AsyncMock(return_value=(True, True))
        hub._event_bus = MagicMock()
        hub._operation_event_bus = MagicMock()
        ws = MagicMock()

        result = await _await(cdc.deregister_worker(hub, "w1", ws))

        assert result == (True, True)
        hub._event_bus.close_worker.assert_called_once_with("w1")
        hub._operation_event_bus.close_worker.assert_called_once_with("w1")

    async def test_no_broadcast_skips_both_event_buses(self) -> None:
        hub = _make_hub()
        hub.connection_mgr.deregister_worker = AsyncMock(return_value=(False, False))
        hub._event_bus = MagicMock()
        hub._operation_event_bus = MagicMock()
        ws = MagicMock()

        result = await _await(cdc.deregister_worker(hub, "w1", ws))

        assert result == (False, False)
        hub._event_bus.close_worker.assert_not_called()
        hub._operation_event_bus.close_worker.assert_not_called()
