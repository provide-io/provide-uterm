#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for hub/ownership.py — extend_lease and get_fresh_expiry."""

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


async def _setup_worker(hub: TermHub, worker_id: str, worker_ws: Any = None) -> WorkerTermState:
    async with hub._lock:
        st = hub._workers.setdefault(worker_id, WorkerTermState())
        st.worker_ws = worker_ws or _make_ws()
        return st


# ===========================================================================
# ownership.py — _expire_leases_under_lock
# ===========================================================================


class TestExtendHijackLease:
    async def test_last_heartbeat_updated_to_now(self) -> None:
        """mutmut_8: last_heartbeat = now → None.  Must be a float after extend."""
        hub = _make_hub()
        now = time.time()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_session = _make_hijack_session(hijack_id="hj-1", lease_s=60.0)
            st.hijack_session.last_heartbeat = 0.0  # old value

        result = await hub.extend_hijack_lease("w1", "hj-1", 30, now)
        assert result is not None
        async with hub._lock:
            st2 = hub._workers["w1"]
            assert st2.hijack_session is not None
            assert st2.hijack_session.last_heartbeat == now


# ===========================================================================
# ownership.py — get_fresh_hijack_expiry
# ===========================================================================


class TestGetFreshHijackExpiry:
    async def test_returns_session_expiry_not_fallback(self) -> None:
        """mutmut_1/2: st = None or workers.get(None) — must use worker_id."""
        hub = _make_hub()
        now = time.time()
        expected_expiry = now + 500.0
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_session = _make_hijack_session(hijack_id="hj-1")
            st.hijack_session.lease_expires_at = expected_expiry

        result = await hub.get_fresh_hijack_expiry("w1", "hj-1", fallback=0.0)
        assert result == expected_expiry, f"Expected session expiry {expected_expiry}, got {result}"

    async def test_returns_fallback_when_worker_absent(self) -> None:
        """mutmut_5: st is not None → is None (inverts check).

        With mutation, would try to access st.hijack_session on None → AttributeError.
        """
        hub = _make_hub()
        fallback = 12345.0
        result = await hub.get_fresh_hijack_expiry("nonexistent", "hj-1", fallback=fallback)
        assert result == fallback

    async def test_returns_fallback_when_hijack_id_mismatch(self) -> None:
        """mutmut_7: hijack_id == hijack_id → != hijack_id.

        With mutation, a matching ID returns fallback; a non-matching one returns expiry.
        """
        hub = _make_hub()
        now = time.time()
        expected_expiry = now + 500.0
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_session = _make_hijack_session(hijack_id="hj-1")
            st.hijack_session.lease_expires_at = expected_expiry

        # Matching ID → must return session expiry (not fallback)
        result = await hub.get_fresh_hijack_expiry("w1", "hj-1", fallback=0.0)
        assert result == expected_expiry

        # Non-matching ID → must return fallback
        result2 = await hub.get_fresh_hijack_expiry("w1", "hj-WRONG", fallback=0.0)
        assert result2 == 0.0


# ===========================================================================
# ownership.py — get_hijack_events_data
# ===========================================================================
