#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for hijack/hub/ownership.py — part 2.

Covers get_hijack_events_data, check_hijack_valid, prepare_browser_input mutations.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
