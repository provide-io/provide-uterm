#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for hijack/hub — events, broadcast, and hijack state messaging.

Targets surviving mutants in:
- ownership.py: get_hijack_events_data
- core.py: notify_hijack_changed, broadcast, hijack_state_msg_for, _resolve_role_for_browser
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.models import HijackSession, WorkerTermState

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_hub(**kwargs: Any) -> TermHub:
    return TermHub(**kwargs)


def _make_ws() -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


def _make_hijack_session(hijack_id: str = "hj-1", owner: str = "op", lease_s: float = 60.0) -> HijackSession:
    now = time.time()
    return HijackSession(
        hijack_id=hijack_id,
        owner=owner,
        acquired_at=now,
        lease_expires_at=now + lease_s,
        last_heartbeat=now,
    )


# ===========================================================================
# ownership.py — get_hijack_events_data
# ===========================================================================


class TestGetHijackEventsData:
    def _make_hs(self, lease_expiry: float | None = None) -> HijackSession:
        now = time.time()
        return HijackSession(
            hijack_id="hj-1",
            owner="op",
            acquired_at=now,
            lease_expires_at=lease_expiry or (now + 60),
            last_heartbeat=now,
        )

    async def test_result_has_rows_key(self) -> None:
        """mutmut_4/5: 'rows' → 'XXrowsXX'/'ROWS'."""
        hub = _make_hub()
        hs = self._make_hs()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_session = hs

        result = await hub.get_hijack_events_data("w1", "hj-1", hs, after_seq=0, limit=10)
        assert "rows" in result

    async def test_result_has_latest_seq_key(self) -> None:
        """mutmut_6/7: 'latest_seq' → 'XXlatest_seqXX'/'LATEST_SEQ'."""
        hub = _make_hub()
        hs = self._make_hs()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_session = hs

        result = await hub.get_hijack_events_data("w1", "hj-1", hs, after_seq=0, limit=10)
        assert "latest_seq" in result

    async def test_result_has_min_event_seq_key(self) -> None:
        """mutmut_9/10: 'min_event_seq' → 'XXmin_event_seqXX'/'MIN_EVENT_SEQ'."""
        hub = _make_hub()
        hs = self._make_hs()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_session = hs

        result = await hub.get_hijack_events_data("w1", "hj-1", hs, after_seq=0, limit=10)
        assert "min_event_seq" in result

    async def test_result_has_fresh_expires_key(self) -> None:
        """mutmut_12/13: 'fresh_expires' → 'XXfresh_expiresXX'/'FRESH_EXPIRES'."""
        hub = _make_hub()
        hs = self._make_hs()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_session = hs

        result = await hub.get_hijack_events_data("w1", "hj-1", hs, after_seq=0, limit=10)
        assert "fresh_expires" in result

    async def test_latest_seq_is_actual_event_seq(self) -> None:
        """mutmut_25: latest_seq = st.event_seq → None."""
        hub = _make_hub()
        hs = self._make_hs()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_session = hs
            st.event_seq = 42

        result = await hub.get_hijack_events_data("w1", "hj-1", hs, after_seq=0, limit=10)
        assert result["latest_seq"] == 42

    async def test_min_event_seq_is_actual_min_event_seq(self) -> None:
        """mutmut_26: min_event_seq = st.min_event_seq → None."""
        hub = _make_hub()
        hs = self._make_hs()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_session = hs
            st.min_event_seq = 7

        result = await hub.get_hijack_events_data("w1", "hj-1", hs, after_seq=0, limit=10)
        assert result["min_event_seq"] == 7

    async def test_fresh_expires_uses_session_expiry_when_matching(self) -> None:
        """mutmut_27/28/29/30: fresh_expires conditional logic."""
        hub = _make_hub()
        session_expiry = time.time() + 300.0
        hs = self._make_hs(lease_expiry=session_expiry)
        hs_fallback = self._make_hs(lease_expiry=99.0)
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_session = hs

        result = await hub.get_hijack_events_data("w1", "hj-1", hs_fallback, after_seq=0, limit=10)
        assert result["fresh_expires"] == session_expiry

    async def test_fresh_expires_uses_fallback_hs_when_session_id_differs(self) -> None:
        """mutmut_30: == → != hijack_id, causes wrong expiry selection."""
        hub = _make_hub()
        fallback_expiry = 99.0
        session_expiry = time.time() + 300.0
        hs_passed = HijackSession(
            hijack_id="hj-OTHER",
            owner="op",
            acquired_at=time.time(),
            lease_expires_at=fallback_expiry,
            last_heartbeat=time.time(),
        )
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_session = _make_hijack_session(hijack_id="hj-1")
            st.hijack_session.lease_expires_at = session_expiry

        result = await hub.get_hijack_events_data("w1", "hj-OTHER", hs_passed, after_seq=0, limit=10)
        assert result["fresh_expires"] == fallback_expiry

    async def test_seq_filter_gt_not_gte(self) -> None:
        """mutmut_23: seq filter > after_seq → >= would include after_seq itself."""
        hub = _make_hub()
        hs = self._make_hs()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_session = hs

        await hub.append_event("w1", "test_event")  # seq=1

        result = await hub.get_hijack_events_data("w1", "hj-1", hs, after_seq=1, limit=10)
        assert result["rows"] == []

    async def test_seq_filter_with_default_zero(self) -> None:
        """mutmut_18/20: evt.get('seq', 0) → None or missing arg."""
        hub = _make_hub()
        hs = self._make_hs()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_session = hs

        await hub.append_event("w1", "ev1")  # seq=1

        result = await hub.get_hijack_events_data("w1", "hj-1", hs, after_seq=0, limit=10)
        assert len(result["rows"]) == 1


# ===========================================================================
# core.py — notify_hijack_changed
# ===========================================================================
