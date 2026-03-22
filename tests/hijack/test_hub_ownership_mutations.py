#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for hijack/hub/ownership.py.

Targets survived mutants in _HijackOwnershipMixin methods.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from tests.hijack.control_stream_helpers import decode_control_payload
from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.models import HijackSession, WorkerTermState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hub() -> TermHub:
    return TermHub()


def _make_ws() -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


async def _register_worker(hub: TermHub, worker_id: str, worker_ws: Any | None = None) -> WorkerTermState:
    async with hub._lock:
        st = hub._workers.setdefault(worker_id, WorkerTermState())
        if worker_ws is not None:
            st.worker_ws = worker_ws
        return st


def _make_hijack_session(worker_id: str, lease_s: float = 60.0) -> HijackSession:
    now = time.time()
    return HijackSession(
        hijack_id=f"hj-{worker_id}",
        owner="test-owner",
        acquired_at=now,
        lease_expires_at=now + lease_s,
        last_heartbeat=now,
    )


# ---------------------------------------------------------------------------
# _expire_leases_under_lock mutations (mutmut_7, 9, 13, 18, 21, 23, 27, 28)
# ---------------------------------------------------------------------------


class TestExpireLeasesUnderLock:
    async def test_rest_expired_initial_value_is_false(self) -> None:
        """mutmut_7: rest_expired initial value must be False, not None."""
        hub = _make_hub()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        hs = _make_hijack_session("w1", lease_s=60.0)
        async with hub._lock:
            st.hijack_session = hs
        # Session not expired — should return (False, False, False)
        result = await hub._expire_leases_under_lock("w1", time.time() - 10)
        assert result is not None
        rest_expired, dashboard_expired, should_resume = result
        assert rest_expired is False

    async def test_dashboard_expired_initial_value_is_false(self) -> None:
        """mutmut_9: dashboard_expired initial value must be False, not None."""
        hub = _make_hub()
        wws = _make_ws()
        owner_ws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        async with hub._lock:
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 60
        result = await hub._expire_leases_under_lock("w1", time.time() - 10)
        assert result is not None
        rest_expired, dashboard_expired, should_resume = result
        assert dashboard_expired is False

    async def test_rest_lease_expires_at_boundary(self) -> None:
        """mutmut_13: lease_expires_at <= now must expire (not just <)."""
        hub = _make_hub()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        now = time.time()
        hs = HijackSession(
            hijack_id="hj-1",
            owner="test",
            acquired_at=now - 10,
            lease_expires_at=now,  # exactly at boundary
            last_heartbeat=now,
        )
        async with hub._lock:
            st.hijack_session = hs
        # lease_expires_at == now: should expire (<=)
        result = await hub._expire_leases_under_lock("w1", now)
        assert result is not None
        rest_expired, _, _ = result
        assert rest_expired is True

    async def test_dashboard_lease_expires_at_boundary(self) -> None:
        """mutmut_21: hijack_owner_expires_at <= now must expire (not just <)."""
        hub = _make_hub()
        wws = _make_ws()
        owner_ws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        now = time.time()
        async with hub._lock:
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = now  # exactly at boundary

        result = await hub._expire_leases_under_lock("w1", now)
        assert result is not None
        _, dashboard_expired, _ = result
        assert dashboard_expired is True

    async def test_owner_expires_at_set_to_none_not_empty(self) -> None:
        """mutmut_23: hijack_owner_expires_at must be set to None, not ''."""
        hub = _make_hub()
        wws = _make_ws()
        owner_ws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        now = time.time()
        async with hub._lock:
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = now - 1  # already expired

        await hub._expire_leases_under_lock("w1", now)
        async with hub._lock:
            assert hub._workers["w1"].hijack_owner_expires_at is None

    async def test_should_resume_requires_all_cleared(self) -> None:
        """mutmut_27,28: should_resume requires BOTH owner and session cleared."""
        hub = _make_hub()
        wws = _make_ws()
        _make_ws()
        st = await _register_worker(hub, "w1", wws)
        now = time.time()
        # Both rest and dashboard expired, but also a new owner appears (concurrent acquire)
        hs = HijackSession(
            hijack_id="hj-1",
            owner="test",
            acquired_at=now - 10,
            lease_expires_at=now - 1,  # expired
            last_heartbeat=now - 5,
        )
        new_owner = _make_ws()
        async with hub._lock:
            st.hijack_session = hs
            st.hijack_owner = new_owner  # concurrent new owner
            st.hijack_owner_expires_at = now + 60

        result = await hub._expire_leases_under_lock("w1", now)
        assert result is not None
        rest_expired, _, should_resume = result
        assert rest_expired is True
        # should_resume must be False because owner is still set
        assert should_resume is False


# ---------------------------------------------------------------------------
# _recheck_and_resume mutations (mutmut_18-32)
# ---------------------------------------------------------------------------


class TestRecheckAndResume:
    async def test_sends_resume_with_correct_owner_field(self) -> None:
        """mutmut_18-21: resume message must have 'owner' key with 'lease-expired' value."""
        hub = _make_hub()
        wws = _make_ws()
        await _register_worker(hub, "w1", wws)
        now = time.time()
        await hub._recheck_and_resume("w1", now)
        wws.send_text.assert_called()
        msg = decode_control_payload(wws.send_text.call_args[0][0])
        assert "owner" in msg
        assert msg["owner"] == "lease-expired"
        assert msg["action"] == "resume"

    async def test_sends_resume_with_lease_s_zero(self) -> None:
        """mutmut_22-24: resume message must have 'lease_s' key with value 0."""
        hub = _make_hub()
        wws = _make_ws()
        await _register_worker(hub, "w1", wws)
        now = time.time()
        await hub._recheck_and_resume("w1", now)
        msg = decode_control_payload(wws.send_text.call_args[0][0])
        assert "lease_s" in msg
        assert msg["lease_s"] == 0

    async def test_sends_resume_with_ts_field(self) -> None:
        """mutmut_25,26: resume message must have 'ts' key."""
        hub = _make_hub()
        wws = _make_ws()
        await _register_worker(hub, "w1", wws)
        now = time.time()
        await hub._recheck_and_resume("w1", now)
        msg = decode_control_payload(wws.send_text.call_args[0][0])
        assert "ts" in msg

    async def test_notify_hijack_changed_called_with_worker_id(self) -> None:
        """mutmut_27: notify_hijack_changed must be called with worker_id, not None."""
        hub = _make_hub()
        wws = _make_ws()
        await _register_worker(hub, "w1", wws)
        now = time.time()
        with patch.object(hub, "notify_hijack_changed") as mock_notify:
            await hub._recheck_and_resume("w1", now)
            mock_notify.assert_called_once()
            assert mock_notify.call_args[0][0] == "w1"

    async def test_notify_hijack_changed_called_with_enabled_false(self) -> None:
        """mutmut_28,32: notify_hijack_changed must be called with enabled=False."""
        hub = _make_hub()
        wws = _make_ws()
        await _register_worker(hub, "w1", wws)
        now = time.time()
        with patch.object(hub, "notify_hijack_changed") as mock_notify:
            await hub._recheck_and_resume("w1", now)
            mock_notify.assert_called_once()
            assert mock_notify.call_args[1]["enabled"] is False

    async def test_notify_hijack_changed_called_with_owner_none(self) -> None:
        """mutmut_31: notify_hijack_changed must be called with owner=None."""
        hub = _make_hub()
        wws = _make_ws()
        await _register_worker(hub, "w1", wws)
        now = time.time()
        with patch.object(hub, "notify_hijack_changed") as mock_notify:
            await hub._recheck_and_resume("w1", now)
            mock_notify.assert_called_once()
            assert mock_notify.call_args[1]["owner"] is None


# ---------------------------------------------------------------------------
# cleanup_expired_hijack mutations (mutmut_18, 21-22, 25-28, 31-32)
# ---------------------------------------------------------------------------


class TestCleanupExpiredHijack:
    async def test_recheck_called_with_worker_id_not_none(self) -> None:
        """mutmut_18: _recheck_and_resume must be called with worker_id, not None."""
        hub = _make_hub()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        now = time.time()
        hs = HijackSession(
            hijack_id="hj-1",
            owner="test",
            acquired_at=now - 10,
            lease_expires_at=now - 1,  # expired
            last_heartbeat=now - 5,
        )
        async with hub._lock:
            st.hijack_session = hs

        # Track what _recheck_and_resume was called with
        called_with = []

        async def mock_recheck(worker_id, ts):
            called_with.append(worker_id)
            # Don't call original to avoid actually sending

        hub._recheck_and_resume = mock_recheck  # type: ignore
        await hub.cleanup_expired_hijack("w1")
        assert "w1" in called_with

    async def test_append_event_for_rest_expiry_uses_worker_id(self) -> None:
        """mutmut_21,22,25,26: append_event must be called with worker_id and correct event name."""
        hub = _make_hub()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        now = time.time()
        hs = HijackSession(
            hijack_id="hj-1",
            owner="test",
            acquired_at=now - 10,
            lease_expires_at=now - 1,  # expired
            last_heartbeat=now - 5,
        )
        async with hub._lock:
            st.hijack_session = hs
            # Prevent resume from sending
            st.worker_ws = None

        events = []

        async def mock_append(wid, event_name, *args, **kwargs):
            events.append((wid, event_name))

        hub.append_event = mock_append  # type: ignore
        await hub.cleanup_expired_hijack("w1")
        # Should have been called with ("w1", "hijack_lease_expired")
        assert ("w1", "hijack_lease_expired") in events

    async def test_append_event_for_dashboard_expiry_uses_worker_id(self) -> None:
        """mutmut_27,28,31,32: append_event for dashboard expiry uses worker_id and correct name."""
        hub = _make_hub()
        wws = _make_ws()
        owner_ws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        now = time.time()
        async with hub._lock:
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = now - 1  # expired

        events = []

        async def mock_append(wid, event_name, *args, **kwargs):
            events.append((wid, event_name))

        hub.append_event = mock_append  # type: ignore
        await hub.cleanup_expired_hijack("w1")
        assert ("w1", "hijack_owner_expired") in events
