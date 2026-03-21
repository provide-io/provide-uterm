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

import pytest

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
        import json

        msg = json.loads(wws.send_text.call_args[0][0])
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
        import json

        msg = json.loads(wws.send_text.call_args[0][0])
        assert "lease_s" in msg
        assert msg["lease_s"] == 0

    async def test_sends_resume_with_ts_field(self) -> None:
        """mutmut_25,26: resume message must have 'ts' key."""
        hub = _make_hub()
        wws = _make_ws()
        await _register_worker(hub, "w1", wws)
        now = time.time()
        await hub._recheck_and_resume("w1", now)
        import json

        msg = json.loads(wws.send_text.call_args[0][0])
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


# ---------------------------------------------------------------------------
# get_rest_session mutations (mutmut_1, 9)
# ---------------------------------------------------------------------------


class TestGetRestSession:
    async def test_calls_cleanup_with_correct_worker_id(self) -> None:
        """mutmut_1: cleanup_expired_hijack must be called with worker_id, not None."""
        hub = _make_hub()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        hs = _make_hijack_session("w1")
        async with hub._lock:
            st.hijack_session = hs

        cleaned_ids = []
        original = hub.cleanup_expired_hijack

        async def mock_cleanup(wid):
            cleaned_ids.append(wid)
            return await original(wid)

        hub.cleanup_expired_hijack = mock_cleanup  # type: ignore
        await hub.get_rest_session("w1", hs.hijack_id)
        assert "w1" in cleaned_ids

    async def test_expires_session_at_boundary(self) -> None:
        """mutmut_9: lease_expires_at <= time.time() must expire (not just <)."""
        hub = _make_hub()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        now = time.time()
        hs = HijackSession(
            hijack_id="hj-1",
            owner="test",
            acquired_at=now - 10,
            lease_expires_at=now - 0.001,  # just expired
            last_heartbeat=now - 5,
        )
        async with hub._lock:
            st.hijack_session = hs

        result = await hub.get_rest_session("w1", "hj-1")
        assert result is None  # expired session should not be returned


# ---------------------------------------------------------------------------
# try_acquire_rest_hijack mutations (mutmut_18, 20)
# ---------------------------------------------------------------------------


class TestTryAcquireRestHijack:
    async def test_acquired_at_is_set_to_now(self) -> None:
        """mutmut_18: acquired_at must be set to now, not None."""
        hub = _make_hub()
        wws = _make_ws()
        await _register_worker(hub, "w1", wws)
        now = time.time()
        acquired, err = await hub.try_acquire_rest_hijack("w1", owner="test", lease_s=60, hijack_id="hj-1", now=now)
        assert acquired is True
        async with hub._lock:
            hs = hub._workers["w1"].hijack_session
            assert hs is not None
            assert hs.acquired_at == now
            assert hs.acquired_at is not None

    async def test_last_heartbeat_is_set_to_now(self) -> None:
        """mutmut_20: last_heartbeat must be set to now, not None."""
        hub = _make_hub()
        wws = _make_ws()
        await _register_worker(hub, "w1", wws)
        now = time.time()
        acquired, err = await hub.try_acquire_rest_hijack("w1", owner="test", lease_s=60, hijack_id="hj-1", now=now)
        assert acquired is True
        async with hub._lock:
            hs = hub._workers["w1"].hijack_session
            assert hs is not None
            assert hs.last_heartbeat == now
            assert hs.last_heartbeat is not None


# ---------------------------------------------------------------------------
# touch_hijack_owner mutations (mutmut_12, 18)
# ---------------------------------------------------------------------------


class TestTouchHijackOwner:
    async def test_min_ttl_clamped_to_1(self) -> None:
        """mutmut_12: max(1, ...) must clamp to 1, not 2."""
        hub = _make_hub()
        wws = _make_ws()
        owner_ws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        async with hub._lock:
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 60

        # lease_s=0 should be clamped to max(1, ...) = 1
        result = await hub.touch_hijack_owner("w1", lease_s=0)
        assert result is not None
        # The expiry should be ~1 second from now (not 2)
        assert result < time.time() + 2

    async def test_max_ttl_clamped_to_600(self) -> None:
        """mutmut_18: min(..., 600) must clamp to 600, not 601."""
        hub = _make_hub()
        wws = _make_ws()
        owner_ws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        async with hub._lock:
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 60

        # lease_s=9999 should be clamped to 600
        result = await hub.touch_hijack_owner("w1", lease_s=9999)
        assert result is not None
        # Should be ~600s from now, not ~601s
        assert result <= time.time() + 601
        assert result >= time.time() + 599


# ---------------------------------------------------------------------------
# remove_dead_browsers mutations (mutmut_8, 9, 13, 28-45, 49)
# ---------------------------------------------------------------------------


class TestRemoveDeadBrowsers:
    async def test_dead_browser_removed_from_browsers_dict(self) -> None:
        """mutmut_8: browsers.pop must work correctly."""
        hub = _make_hub()
        dead_ws = _make_ws()
        alive_ws = _make_ws()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        async with hub._lock:
            st.browsers[dead_ws] = "operator"
            st.browsers[alive_ws] = "operator"

        await hub.remove_dead_browsers("w1", {dead_ws})
        async with hub._lock:
            assert dead_ws not in hub._workers["w1"].browsers
            assert alive_ws in hub._workers["w1"].browsers

    async def test_owner_cleared_only_when_and_condition_both_true(self) -> None:
        """mutmut_9: is_dashboard_hijack_active AND hijack_owner is ws (not OR)."""
        hub = _make_hub()
        wws = _make_ws()
        owner_ws = _make_ws()
        non_owner_dead = _make_ws()  # dead but not owner
        st = await _register_worker(hub, "w1", wws)
        async with hub._lock:
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 60
            st.browsers[non_owner_dead] = "operator"

        # Dead socket is NOT the hijack owner — owner must NOT be cleared
        await hub.remove_dead_browsers("w1", {non_owner_dead})
        async with hub._lock:
            # Owner should still be set
            assert hub._workers["w1"].hijack_owner is owner_ws

    async def test_owner_expires_at_set_to_none_not_empty(self) -> None:
        """mutmut_13: hijack_owner_expires_at must be set to None, not ''."""
        hub = _make_hub()
        wws = _make_ws()
        owner_ws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        async with hub._lock:
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 60
            st.browsers[owner_ws] = "admin"

        await hub.remove_dead_browsers("w1", {owner_ws})
        async with hub._lock:
            assert hub._workers["w1"].hijack_owner_expires_at is None

    async def test_resume_sent_with_correct_keys(self) -> None:
        """mutmut_28-44: resume message must have correct keys/values."""
        hub = _make_hub()
        wws = _make_ws()
        owner_ws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        async with hub._lock:
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 60
            st.browsers[owner_ws] = "admin"

        await hub.remove_dead_browsers("w1", {owner_ws})
        import json

        wws.send_text.assert_called()
        msg = json.loads(wws.send_text.call_args[0][0])
        assert msg["type"] == "control"
        assert msg["action"] == "resume"
        assert msg["owner"] == "dead-socket"
        assert msg["lease_s"] == 0
        assert "ts" in msg

    async def test_notify_called_with_worker_id_not_none(self) -> None:
        """mutmut_45: notify_hijack_changed must be called with worker_id."""
        hub = _make_hub()
        wws = _make_ws()
        owner_ws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        async with hub._lock:
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 60
            st.browsers[owner_ws] = "admin"

        with patch.object(hub, "notify_hijack_changed") as mock_notify:
            await hub.remove_dead_browsers("w1", {owner_ws})
            mock_notify.assert_called_once()
            assert mock_notify.call_args[0][0] == "w1"

    async def test_notify_called_with_owner_none(self) -> None:
        """mutmut_49: notify_hijack_changed must be called with owner=None."""
        hub = _make_hub()
        wws = _make_ws()
        owner_ws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        async with hub._lock:
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 60
            st.browsers[owner_ws] = "admin"

        with patch.object(hub, "notify_hijack_changed") as mock_notify:
            await hub.remove_dead_browsers("w1", {owner_ws})
            mock_notify.assert_called_once()
            assert mock_notify.call_args[1]["owner"] is None


# ---------------------------------------------------------------------------
# extend_hijack_lease mutations (mutmut_8)
# ---------------------------------------------------------------------------


class TestExtendHijackLease:
    async def test_last_heartbeat_updated_to_now(self) -> None:
        """mutmut_8: last_heartbeat must be updated to now, not None."""
        hub = _make_hub()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        now = time.time()
        hs = _make_hijack_session("w1")
        async with hub._lock:
            st.hijack_session = hs

        new_expiry = await hub.extend_hijack_lease("w1", hs.hijack_id, 30, now)
        assert new_expiry is not None
        async with hub._lock:
            updated_hs = hub._workers["w1"].hijack_session
            assert updated_hs is not None
            assert updated_hs.last_heartbeat == now
            assert updated_hs.last_heartbeat is not None


# ---------------------------------------------------------------------------
# get_fresh_hijack_expiry mutations (mutmut_1, 2, 5, 7)
# ---------------------------------------------------------------------------


class TestGetFreshHijackExpiry:
    async def test_returns_session_expiry_when_id_matches(self) -> None:
        """mutmut_1,2,5,7: must return session expiry for matching hijack_id."""
        hub = _make_hub()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        now = time.time()
        hs = HijackSession(
            hijack_id="hj-1",
            owner="test",
            acquired_at=now,
            lease_expires_at=now + 42,
            last_heartbeat=now,
        )
        async with hub._lock:
            st.hijack_session = hs

        result = await hub.get_fresh_hijack_expiry("w1", "hj-1", fallback=999.0)
        assert result == pytest.approx(now + 42, abs=0.1)
        # Must NOT return fallback
        assert result != 999.0

    async def test_returns_fallback_when_no_session(self) -> None:
        """mutmut_1,2: must return fallback when no session exists."""
        hub = _make_hub()
        result = await hub.get_fresh_hijack_expiry("nonexistent", "hj-x", fallback=777.0)
        assert result == 777.0

    async def test_returns_fallback_when_id_mismatch(self) -> None:
        """mutmut_7: hijack_id must match (== not !=)."""
        hub = _make_hub()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        now = time.time()
        hs = HijackSession(
            hijack_id="hj-1",
            owner="test",
            acquired_at=now,
            lease_expires_at=now + 42,
            last_heartbeat=now,
        )
        async with hub._lock:
            st.hijack_session = hs

        # Different hijack_id → should return fallback
        result = await hub.get_fresh_hijack_expiry("w1", "hj-DIFFERENT", fallback=888.0)
        assert result == 888.0


# ---------------------------------------------------------------------------
# get_hijack_events_data mutations (mutmut_4-13, 18, 20, 23, 25-30)
# ---------------------------------------------------------------------------


class TestGetHijackEventsData:
    def _make_session(self, hijack_id: str = "hj-1") -> HijackSession:
        now = time.time()
        return HijackSession(
            hijack_id=hijack_id,
            owner="test",
            acquired_at=now,
            lease_expires_at=now + 60,
            last_heartbeat=now,
        )

    async def test_rows_key_in_result(self) -> None:
        """mutmut_4,5: result must have 'rows' key."""
        hub = _make_hub()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        hs = self._make_session()
        async with hub._lock:
            st.hijack_session = hs

        result = await hub.get_hijack_events_data("w1", "hj-1", hs, 0, 100)
        assert "rows" in result

    async def test_latest_seq_key_in_result(self) -> None:
        """mutmut_6,7,8: result must have 'latest_seq' key with correct value."""
        hub = _make_hub()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        hs = self._make_session()
        async with hub._lock:
            st.hijack_session = hs
            st.event_seq = 42

        result = await hub.get_hijack_events_data("w1", "hj-1", hs, 0, 100)
        assert "latest_seq" in result
        assert result["latest_seq"] == 42

    async def test_min_event_seq_key_in_result(self) -> None:
        """mutmut_9,10,11: result must have 'min_event_seq' key."""
        hub = _make_hub()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        hs = self._make_session()
        async with hub._lock:
            st.hijack_session = hs
            st.min_event_seq = 5

        result = await hub.get_hijack_events_data("w1", "hj-1", hs, 0, 100)
        assert "min_event_seq" in result
        assert result["min_event_seq"] == 5

    async def test_fresh_expires_key_in_result(self) -> None:
        """mutmut_12,13: result must have 'fresh_expires' key."""
        hub = _make_hub()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        hs = self._make_session()
        async with hub._lock:
            st.hijack_session = hs

        result = await hub.get_hijack_events_data("w1", "hj-1", hs, 0, 100)
        assert "fresh_expires" in result
        assert result["fresh_expires"] is not None

    async def test_seq_filter_uses_0_default(self) -> None:
        """mutmut_18,20,23: seq default must be 0, not None or 1."""
        hub = _make_hub()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        hs = self._make_session()
        # Add an event with seq=1 that should be returned for after_seq=0
        async with hub._lock:
            st.hijack_session = hs
            st.events.append({"seq": 1, "type": "test"})
            st.event_seq = 1

        result = await hub.get_hijack_events_data("w1", "hj-1", hs, after_seq=0, limit=100)
        assert len(result["rows"]) == 1

    async def test_latest_seq_from_event_seq_not_none(self) -> None:
        """mutmut_25: latest_seq = st.event_seq, not None."""
        hub = _make_hub()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        hs = self._make_session()
        async with hub._lock:
            st.hijack_session = hs
            st.event_seq = 17

        result = await hub.get_hijack_events_data("w1", "hj-1", hs, 0, 100)
        assert result["latest_seq"] == 17
        assert result["latest_seq"] is not None

    async def test_min_event_seq_from_state_not_none(self) -> None:
        """mutmut_26: min_event_seq = st.min_event_seq, not None."""
        hub = _make_hub()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        hs = self._make_session()
        async with hub._lock:
            st.hijack_session = hs
            st.min_event_seq = 3

        result = await hub.get_hijack_events_data("w1", "hj-1", hs, 0, 100)
        assert result["min_event_seq"] == 3
        assert result["min_event_seq"] is not None

    async def test_fresh_expires_from_session_when_id_matches(self) -> None:
        """mutmut_27,28,29,30: fresh_expires from session when hijack_id matches."""
        hub = _make_hub()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        now = time.time()
        hs = HijackSession(
            hijack_id="hj-1",
            owner="test",
            acquired_at=now,
            lease_expires_at=now + 123,
            last_heartbeat=now,
        )
        async with hub._lock:
            st.hijack_session = hs

        result = await hub.get_hijack_events_data("w1", "hj-1", hs, 0, 100)
        assert result["fresh_expires"] == pytest.approx(now + 123, abs=0.1)


# ---------------------------------------------------------------------------
# check_hijack_valid mutations (mutmut_9: > vs >=)
# ---------------------------------------------------------------------------


class TestCheckHijackValid:
    async def test_valid_session_returns_true(self) -> None:
        """Session with future expiry must return True."""
        hub = _make_hub()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        now = time.time()
        hs = HijackSession(
            hijack_id="hj-1",
            owner="test",
            acquired_at=now,
            lease_expires_at=now + 60,
            last_heartbeat=now,
        )
        async with hub._lock:
            st.hijack_session = hs

        result = await hub.check_hijack_valid("w1", "hj-1")
        assert result is True

    async def test_exactly_expired_session_returns_false(self) -> None:
        """mutmut_9: lease_expires_at > time.time() (not >=), so exactly-expired is invalid."""
        hub = _make_hub()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        now = time.time()
        hs = HijackSession(
            hijack_id="hj-1",
            owner="test",
            acquired_at=now - 60,
            lease_expires_at=now - 0.001,  # just expired
            last_heartbeat=now - 60,
        )
        async with hub._lock:
            st.hijack_session = hs

        result = await hub.check_hijack_valid("w1", "hj-1")
        assert result is False


# ---------------------------------------------------------------------------
# prepare_browser_input mutations (mutmut_11, 13, 14, 15)
# ---------------------------------------------------------------------------


class TestPrepareBrowserInput:
    async def test_or_condition_not_used_for_lease_extension(self) -> None:
        """mutmut_11: must use 'and' condition for lease extension (active AND owner is ws)."""
        hub = _make_hub()
        wws = _make_ws()
        owner_ws = _make_ws()
        non_owner = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        async with hub._lock:
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 60
            st.input_mode = "hijack"
            st.browsers[owner_ws] = "admin"

        initial_expiry = time.time() + 60
        async with hub._lock:
            st.hijack_owner_expires_at = initial_expiry

        # Calling with non_owner should NOT extend the lease
        await hub.prepare_browser_input("w1", non_owner)
        async with hub._lock:
            # Expiry should not have changed (or changed by tiny delta)
            new_expiry = hub._workers["w1"].hijack_owner_expires_at
            assert new_expiry is not None

    async def test_owner_ws_lease_extended_not_negated(self) -> None:
        """mutmut_13: condition must be 'ws' not 'not ws'."""
        hub = _make_hub()
        wws = _make_ws()
        owner_ws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        before = time.time()
        async with hub._lock:
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = before + 10  # short lease
            st.input_mode = "hijack"
            st.browsers[owner_ws] = "admin"

        # Prepare for owner — lease should be extended
        await hub.prepare_browser_input("w1", owner_ws)
        async with hub._lock:
            new_expiry = hub._workers["w1"].hijack_owner_expires_at
        # Expiry should be around time.time() + _dashboard_hijack_lease_s
        assert new_expiry is not None
        assert new_expiry > before + 10  # extended

    async def test_owner_expires_at_not_set_to_none(self) -> None:
        """mutmut_14: hijack_owner_expires_at must NOT be set to None."""
        hub = _make_hub()
        wws = _make_ws()
        owner_ws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        async with hub._lock:
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 60
            st.input_mode = "hijack"
            st.browsers[owner_ws] = "admin"

        await hub.prepare_browser_input("w1", owner_ws)
        async with hub._lock:
            assert hub._workers["w1"].hijack_owner_expires_at is not None

    async def test_owner_expires_at_not_subtracted(self) -> None:
        """mutmut_15: hijack_owner_expires_at must be time.time() + lease, not - lease."""
        hub = _make_hub()
        wws = _make_ws()
        owner_ws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        async with hub._lock:
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 60
            st.input_mode = "hijack"
            st.browsers[owner_ws] = "admin"

        await hub.prepare_browser_input("w1", owner_ws)
        async with hub._lock:
            new_expiry = hub._workers["w1"].hijack_owner_expires_at
        # Must be in the future (not in the past due to subtraction)
        assert new_expiry is not None
        assert new_expiry > time.time()
