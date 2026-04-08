#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for hijack/hub/ownership.py (part 2).

Targets survived mutants in _HijackOwnershipMixin methods.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.bridge.control_channel_helpers import decode_control_payload
from provide.terminal.bridge.hub import TermHub
from provide.terminal.bridge.models import HijackSession, WorkerTermState

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
    now = time.monotonic()
    return HijackSession(
        hijack_id=f"hj-{worker_id}",
        owner="test-owner",
        acquired_at=now,
        lease_expires_at=now + lease_s,
        last_heartbeat=now,
    )


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
        """mutmut_9: lease_expires_at <= time.monotonic() must expire (not just <)."""
        hub = _make_hub()
        wws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        now = time.monotonic()
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
        now = time.monotonic()
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
        now = time.monotonic()
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
            st.hijack_owner_expires_at = time.monotonic() + 60

        # lease_s=0 should be clamped to max(1, ...) = 1
        result = await hub.touch_hijack_owner("w1", lease_s=0)
        assert result is not None
        # The expiry should be ~1 second from now (not 2)
        assert result < time.monotonic() + 2

    async def test_max_ttl_clamped_to_600(self) -> None:
        """mutmut_18: min(..., 600) must clamp to 600, not 601."""
        hub = _make_hub()
        wws = _make_ws()
        owner_ws = _make_ws()
        st = await _register_worker(hub, "w1", wws)
        async with hub._lock:
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.monotonic() + 60

        # lease_s=9999 should be clamped to 600
        result = await hub.touch_hijack_owner("w1", lease_s=9999)
        assert result is not None
        # Should be ~600s from now, not ~601s
        assert result <= time.monotonic() + 601
        assert result >= time.monotonic() + 599


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
            st.hijack_owner_expires_at = time.monotonic() + 60
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
            st.hijack_owner_expires_at = time.monotonic() + 60
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
            st.hijack_owner_expires_at = time.monotonic() + 60
            st.browsers[owner_ws] = "admin"

        await hub.remove_dead_browsers("w1", {owner_ws})

        wws.send_text.assert_called()
        msg = decode_control_payload(wws.send_text.call_args[0][0])
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
            st.hijack_owner_expires_at = time.monotonic() + 60
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
            st.hijack_owner_expires_at = time.monotonic() + 60
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
        now = time.monotonic()
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
        now = time.monotonic()
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
        now = time.monotonic()
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
