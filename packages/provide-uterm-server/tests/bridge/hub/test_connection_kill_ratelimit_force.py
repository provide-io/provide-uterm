#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ConnectionManager rate-limit / token / force-release surfaces.

Targets in
``packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/connection.py``:

* ``allow_rest_acquire_for`` — returns ``hub.limiter.allow_rest_acquire(client_id)``;
  on rejection ONLY, emits ``logger.warning(EVENT_RATE_LIMIT_TRIGGERED,
  client_id=..., limit_type="rest_acquire")``.
* ``allow_rest_send_for`` — returns ``hub.limiter.allow_rest_send(client_id)``;
  on rejection ONLY, emits ``logger.warning(EVENT_RATE_LIMIT_TRIGGERED,
  client_id=..., limit_type="rest_send")``.
* ``worker_token`` — returns ``hub._worker_token`` verbatim.
* ``force_release_hijack`` — clears the active hijack (REST session and/or
  dashboard owner), captures the owner, sends the ``resume`` control frame via
  ``send_worker``, calls ``notify_hijack_changed(enabled=False, owner=None)``,
  ``broadcast_hijack_state``, and returns the had-hijack bool.

Every test constructs a FRESH ``TermHub()`` and pins exact return values, exact
state mutations, and the exact observability calls (logger event constant +
kwargs).
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, call, patch

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.hub.ext import EVENT_RATE_LIMIT_TRIGGERED
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

_LOGGER = "provide.uterm.server.bridge.hub.connection.logger"


def _make_session(owner: str, *, lease_offset: float = 60.0) -> HijackSession:
    now = time.time()
    return HijackSession(
        hijack_id=f"hid-{owner}",
        owner=owner,
        acquired_at=now,
        lease_expires_at=now + lease_offset,
        last_heartbeat=now,
    )


# ===========================================================================
# allow_rest_acquire_for
# ===========================================================================


class TestAllowRestAcquireFor:
    def test_returns_limiter_true_no_warning(self) -> None:
        """Allowed -> returns True (the limiter result) and emits NO warning."""
        hub = TermHub()
        hub.limiter = MagicMock()
        hub.limiter.allow_rest_acquire.return_value = True

        with patch(_LOGGER) as logger:
            result = hub.connection_mgr.allow_rest_acquire_for("client-a")

        assert result is True
        hub.limiter.allow_rest_acquire.assert_called_once_with("client-a")
        # No global send bucket should be consulted for an acquire call.
        hub.limiter.allow_rest_send.assert_not_called()
        logger.warning.assert_not_called()

    def test_returns_limiter_false_with_warning(self) -> None:
        """Rejected -> returns False and emits the exact rate-limit warning."""
        hub = TermHub()
        hub.limiter = MagicMock()
        hub.limiter.allow_rest_acquire.return_value = False

        with patch(_LOGGER) as logger:
            result = hub.connection_mgr.allow_rest_acquire_for("client-b")

        assert result is False
        hub.limiter.allow_rest_acquire.assert_called_once_with("client-b")
        logger.warning.assert_called_once_with(
            EVENT_RATE_LIMIT_TRIGGERED, client_id="client-b", limit_type="rest_acquire"
        )

    def test_passes_through_via_hub_facade(self) -> None:
        """The hub facade forwards to the manager and returns the same bool."""
        hub = TermHub()
        hub.limiter = MagicMock()
        hub.limiter.allow_rest_acquire.return_value = True
        with patch(_LOGGER):
            assert hub.allow_rest_acquire_for("c") is True


# ===========================================================================
# allow_rest_send_for
# ===========================================================================


class TestAllowRestSendFor:
    def test_returns_limiter_true_no_warning(self) -> None:
        """Allowed -> returns True (the limiter result) and emits NO warning."""
        hub = TermHub()
        hub.limiter = MagicMock()
        hub.limiter.allow_rest_send.return_value = True

        with patch(_LOGGER) as logger:
            result = hub.connection_mgr.allow_rest_send_for("client-a")

        assert result is True
        hub.limiter.allow_rest_send.assert_called_once_with("client-a")
        hub.limiter.allow_rest_acquire.assert_not_called()
        logger.warning.assert_not_called()

    def test_returns_limiter_false_with_warning(self) -> None:
        """Rejected -> returns False and emits the exact rate-limit warning."""
        hub = TermHub()
        hub.limiter = MagicMock()
        hub.limiter.allow_rest_send.return_value = False

        with patch(_LOGGER) as logger:
            result = hub.connection_mgr.allow_rest_send_for("client-b")

        assert result is False
        hub.limiter.allow_rest_send.assert_called_once_with("client-b")
        logger.warning.assert_called_once_with(EVENT_RATE_LIMIT_TRIGGERED, client_id="client-b", limit_type="rest_send")

    def test_passes_through_via_hub_facade(self) -> None:
        """The hub facade forwards to the manager and returns the same bool."""
        hub = TermHub()
        hub.limiter = MagicMock()
        hub.limiter.allow_rest_send.return_value = True
        with patch(_LOGGER):
            assert hub.allow_rest_send_for("c") is True


# ===========================================================================
# worker_token
# ===========================================================================


class TestWorkerToken:
    def test_returns_none_by_default(self) -> None:
        """Default hub has no worker token -> returns None."""
        hub = TermHub()
        assert hub.connection_mgr.worker_token() is None

    def test_returns_configured_token(self) -> None:
        """Returns the exact configured token string."""
        hub = TermHub(worker_token="s3cr3t-token")
        assert hub.connection_mgr.worker_token() == "s3cr3t-token"

    def test_reads_through_to_hub_attribute(self) -> None:
        """Reads hub._worker_token live (so a later mutation is reflected)."""
        hub = TermHub(worker_token="first")
        assert hub.connection_mgr.worker_token() == "first"
        hub._worker_token = "second"
        assert hub.connection_mgr.worker_token() == "second"

    def test_passes_through_via_hub_facade(self) -> None:
        """The hub facade forwards to the manager and returns the same value."""
        hub = TermHub(worker_token="facade-tok")
        assert hub.worker_token() == "facade-tok"


# ===========================================================================
# force_release_hijack
# ===========================================================================


def _instrument_hub(hub: TermHub) -> dict[str, MagicMock]:
    """Replace the three delegated side-effect methods with recording mocks."""
    send_worker = AsyncMock(return_value=True)
    notify = MagicMock()
    broadcast_state = AsyncMock()
    hub.send_worker = send_worker  # type: ignore[method-assign]
    hub.notify_hijack_changed = notify  # type: ignore[method-assign]
    hub.broadcast_hijack_state = broadcast_state  # type: ignore[method-assign]
    return {"send_worker": send_worker, "notify": notify, "broadcast_state": broadcast_state}


class TestForceReleaseHijack:
    async def test_unknown_worker_returns_false_no_side_effects(self) -> None:
        """Unknown worker -> returns False, no resume frame / notify / broadcast."""
        hub = TermHub()
        mocks = _instrument_hub(hub)

        result = await hub.connection_mgr.force_release_hijack("nope")

        assert result is False
        mocks["send_worker"].assert_not_called()
        mocks["notify"].assert_not_called()
        mocks["broadcast_state"].assert_not_called()

    async def test_known_worker_no_hijack_returns_false_no_side_effects(self) -> None:
        """Worker exists but holds no hijack -> returns False, no side effects, state untouched."""
        hub = TermHub()
        mocks = _instrument_hub(hub)
        async with hub._lock:
            hub._workers["w1"] = WorkerTermState()

        result = await hub.connection_mgr.force_release_hijack("w1")

        assert result is False
        st = hub._workers["w1"]
        assert st.hijack_session is None
        assert st.hijack_owner is None
        assert st.hijack_owner_expires_at is None
        mocks["send_worker"].assert_not_called()
        mocks["notify"].assert_not_called()
        mocks["broadcast_state"].assert_not_called()

    async def test_rest_session_cleared_owner_captured_and_frame_sent(self) -> None:
        """REST hijack -> session cleared, owner captured into the resume frame, returns True."""
        hub = TermHub()
        mocks = _instrument_hub(hub)
        async with hub._lock:
            st = WorkerTermState()
            st.hijack_session = _make_session("rest-bob")
            hub._workers["w1"] = st

        before = time.time()
        result = await hub.connection_mgr.force_release_hijack("w1")
        after = time.time()

        assert result is True
        assert hub._workers["w1"].hijack_session is None

        # Exactly one resume control frame, owner captured from the session.
        mocks["send_worker"].assert_awaited_once()
        sent_args = mocks["send_worker"].await_args
        assert sent_args.args[0] == "w1"
        frame = sent_args.args[1]
        assert frame["type"] == "control"
        assert frame["action"] == "resume"
        assert frame["owner"] == "rest-bob"
        assert frame["lease_s"] == 0
        assert before <= frame["ts"] <= after
        assert set(frame) == {"type", "action", "owner", "lease_s", "ts"}

        mocks["notify"].assert_called_once_with("w1", enabled=False, owner=None)
        mocks["broadcast_state"].assert_awaited_once_with("w1")

    async def test_dashboard_owner_cleared_default_owner_in_frame(self) -> None:
        """Dashboard-only hijack -> owner fields cleared, frame uses 'server-forced' owner."""
        hub = TermHub()
        mocks = _instrument_hub(hub)
        owner_ws = MagicMock()
        async with hub._lock:
            st = WorkerTermState()
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.monotonic() + 60
            hub._workers["w1"] = st

        result = await hub.connection_mgr.force_release_hijack("w1")

        assert result is True
        st = hub._workers["w1"]
        assert st.hijack_owner is None
        assert st.hijack_owner_expires_at is None

        frame = mocks["send_worker"].await_args.args[1]
        # No REST session, so owner stays the default "server-forced".
        assert frame["owner"] == "server-forced"
        mocks["notify"].assert_called_once_with("w1", enabled=False, owner=None)
        mocks["broadcast_state"].assert_awaited_once_with("w1")

    async def test_owner_with_no_expiry_is_active_and_cleared(self) -> None:
        """Dashboard owner with expires_at=None is active -> cleared, returns True."""
        hub = TermHub()
        mocks = _instrument_hub(hub)
        async with hub._lock:
            st = WorkerTermState()
            st.hijack_owner = MagicMock()
            st.hijack_owner_expires_at = None
            hub._workers["w1"] = st

        result = await hub.connection_mgr.force_release_hijack("w1")

        assert result is True
        assert hub._workers["w1"].hijack_owner is None
        mocks["send_worker"].assert_awaited_once()
        mocks["broadcast_state"].assert_awaited_once_with("w1")

    async def test_both_rest_and_dashboard_session_owner_captured(self) -> None:
        """Both leases present -> both cleared, REST owner wins the frame, returns True."""
        hub = TermHub()
        mocks = _instrument_hub(hub)
        async with hub._lock:
            st = WorkerTermState()
            st.hijack_session = _make_session("rest-alice")
            st.hijack_owner = MagicMock()
            st.hijack_owner_expires_at = time.monotonic() + 60
            hub._workers["w1"] = st

        result = await hub.connection_mgr.force_release_hijack("w1")

        assert result is True
        st = hub._workers["w1"]
        assert st.hijack_session is None
        assert st.hijack_owner is None
        assert st.hijack_owner_expires_at is None

        frame = mocks["send_worker"].await_args.args[1]
        assert frame["owner"] == "rest-alice"
        mocks["notify"].assert_called_once_with("w1", enabled=False, owner=None)
        mocks["broadcast_state"].assert_awaited_once_with("w1")

    async def test_side_effect_ordering(self) -> None:
        """Resume frame is sent before notify and broadcast (sequence matters)."""
        hub = TermHub()
        order: list[str] = []
        send_worker = AsyncMock(side_effect=lambda *a, **k: order.append("send"))
        notify = MagicMock(side_effect=lambda *a, **k: order.append("notify"))
        broadcast = AsyncMock(side_effect=lambda *a, **k: order.append("broadcast"))
        hub.send_worker = send_worker  # type: ignore[method-assign]
        hub.notify_hijack_changed = notify  # type: ignore[method-assign]
        hub.broadcast_hijack_state = broadcast  # type: ignore[method-assign]
        async with hub._lock:
            st = WorkerTermState()
            st.hijack_session = _make_session("seq")
            hub._workers["w1"] = st

        assert await hub.connection_mgr.force_release_hijack("w1") is True
        assert order == ["send", "notify", "broadcast"]

    async def test_via_hub_facade(self) -> None:
        """The async hub facade forwards to the manager and returns True."""
        hub = TermHub()
        _instrument_hub(hub)
        async with hub._lock:
            st = WorkerTermState()
            st.hijack_session = _make_session("facade")
            hub._workers["w1"] = st
        assert await hub.force_release_hijack("w1") is True

    async def test_notify_kwargs_are_exact(self) -> None:
        """notify_hijack_changed must be called with enabled=False AND owner=None (both kwargs)."""
        hub = TermHub()
        mocks = _instrument_hub(hub)
        async with hub._lock:
            st = WorkerTermState()
            st.hijack_session = _make_session("kw")
            hub._workers["w1"] = st

        await hub.connection_mgr.force_release_hijack("w1")

        assert mocks["notify"].call_args == call("w1", enabled=False, owner=None)
