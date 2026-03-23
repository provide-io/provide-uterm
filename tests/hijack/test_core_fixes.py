#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for src/provide/terminal/hijack/hub/core.py — part 1.

Covers:
- browser_count: returns actual browser count (not 0) when workers registered.
- get_last_snapshot: returns the stored snapshot (not None) when one exists.
- get_recent_events: limit=1 returns exactly 1 event; limit=501 capped at 500.
- disconnect_worker: state cleared to None (not ""); returns True/False correctly.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.models import WorkerTermState


def _make_hub(**kwargs: Any) -> TermHub:
    return TermHub(**kwargs)


def _make_async_ws() -> AsyncMock:
    """Return a mock WebSocket with async send_text and close."""
    ws = AsyncMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# browser_count: returns actual count (kills mutmut_1, mutmut_2)
# ---------------------------------------------------------------------------


class TestBrowserCount:
    """browser_count must return len(st.browsers) — not 0 — when browsers exist.

    Kills:
    - mutmut_1: _workers.get(worker_id) → _workers.get(None) → always None → returns 0
    - mutmut_2: similar get() argument mutation
    """

    async def test_returns_actual_count_when_browsers_connected(self) -> None:
        """browser_count returns the real number of connected browsers, not 0."""
        hub = _make_hub()
        browser1 = MagicMock()
        browser2 = MagicMock()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.browsers[browser1] = "operator"
            st.browsers[browser2] = "admin"

        count = await hub.browser_count("w1")

        assert count == 2, (
            f"Expected 2 browsers, got {count} — mutmut_1/2 corrupt the get() key so st is always None → returns 0"
        )

    async def test_returns_zero_for_unknown_worker(self) -> None:
        """browser_count returns 0 when the worker is not registered."""
        hub = _make_hub()
        count = await hub.browser_count("no-such-worker")
        assert count == 0

    async def test_returns_one_after_single_browser_added(self) -> None:
        """browser_count == 1 with exactly one browser connected."""
        hub = _make_hub()
        browser_ws = MagicMock()

        async with hub._lock:
            st = hub._workers.setdefault("w2", WorkerTermState())
            st.browsers[browser_ws] = "viewer"

        assert await hub.browser_count("w2") == 1


# ---------------------------------------------------------------------------
# get_last_snapshot: returns stored snapshot (kills mutmut_1, mutmut_2)
# ---------------------------------------------------------------------------


class TestGetLastSnapshot:
    """get_last_snapshot must return st.last_snapshot — not None — when one is stored.

    Kills:
    - mutmut_1: _workers.get(worker_id) → _workers.get(None) → always None
    - mutmut_2: similar get() argument mutation
    """

    async def test_returns_snapshot_when_present(self) -> None:
        """get_last_snapshot returns the stored snapshot dict, not None."""
        hub = _make_hub()
        snapshot = {"type": "snapshot", "screen": "hello", "ts": time.time()}

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.last_snapshot = snapshot

        result = await hub.get_last_snapshot("w1")

        assert result is snapshot, (
            "Expected the stored snapshot dict, got None — mutmut_1/2 corrupt the get() key so st is always None"
        )

    async def test_returns_none_when_no_snapshot(self) -> None:
        """get_last_snapshot returns None when no snapshot has been stored."""
        hub = _make_hub()

        async with hub._lock:
            hub._workers.setdefault("w1", WorkerTermState())

        result = await hub.get_last_snapshot("w1")
        assert result is None

    async def test_returns_none_for_unknown_worker(self) -> None:
        """get_last_snapshot returns None for an unregistered worker."""
        hub = _make_hub()
        assert await hub.get_last_snapshot("no-such-worker") is None


# ---------------------------------------------------------------------------
# get_recent_events: clamping (kills mutmut_10, mutmut_15)
# ---------------------------------------------------------------------------


class TestGetRecentEvents:
    """get_recent_events clamps limit to [1, 500].

    Kills:
    - mutmut_10: max(1, ...) → max(2, ...) — limit=1 returns 0 events (wrong)
    - mutmut_15: min(limit, 500) → min(limit, 501) — limit=501 returns 501 events (wrong)
    """

    async def _hub_with_events(self, n: int) -> TermHub:
        hub = _make_hub()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            for i in range(n):
                st.events.append({"seq": i + 1, "ts": time.time(), "type": "snapshot", "data": {}})
        return hub

    async def test_limit_1_returns_exactly_one_event(self) -> None:
        """With limit=1 and 5 events, exactly 1 event must be returned.

        Kills mutmut_10: max(2, min(1, 500)) = 2 → slice[-2:] returns 2 events.
        """
        hub = await self._hub_with_events(5)
        result = await hub.get_recent_events("w1", limit=1)
        assert len(result) == 1, (
            f"Expected 1 event with limit=1, got {len(result)} — "
            "mutmut_10 changes max(1,...) to max(2,...) returning 2 events"
        )

    async def test_limit_501_capped_at_500(self) -> None:
        """With limit=501 and 510 events, at most 500 must be returned.

        Kills mutmut_15: min(limit, 501) allows 501 events to slip through.
        """
        hub = await self._hub_with_events(510)
        result = await hub.get_recent_events("w1", limit=501)
        assert len(result) == 500, (
            f"Expected 500 events (capped), got {len(result)} — mutmut_15 changes min(limit,500) to min(limit,501)"
        )

    async def test_limit_500_returns_at_most_500(self) -> None:
        """Boundary: limit=500 with 600 events returns exactly 500."""
        hub = await self._hub_with_events(600)
        result = await hub.get_recent_events("w1", limit=500)
        assert len(result) == 500

    async def test_returns_empty_for_unknown_worker(self) -> None:
        hub = _make_hub()
        assert await hub.get_recent_events("no-such", limit=10) == []

    async def test_returns_most_recent_events(self) -> None:
        """The most recent N events (by seq) are returned, not the oldest."""
        hub = _make_hub()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            for i in range(5):
                st.events.append({"seq": i + 1, "ts": time.time(), "type": "ev", "data": {}})

        result = await hub.get_recent_events("w1", limit=2)
        assert len(result) == 2
        assert result[0]["seq"] == 4
        assert result[1]["seq"] == 5


# ---------------------------------------------------------------------------
# disconnect_worker: state cleanup (kills mutmut_1, 9, 13, 15, 16)
# ---------------------------------------------------------------------------


class TestDisconnectWorkerStateCleanup:
    """disconnect_worker must set worker_ws, hijack_owner, and hijack_owner_expires_at
    to exactly None (not "" or any other value).

    Kills:
    - mutmut_1: _workers.get(worker_id) → get(None) → always None → returns False
    - mutmut_9: st.worker_ws = "" instead of None
    - mutmut_15: st.hijack_owner = "" instead of None
    - mutmut_16: st.hijack_owner_expires_at = "" instead of None
    """

    async def test_returns_true_when_worker_connected(self) -> None:
        """disconnect_worker returns True when a worker was connected.

        Kills mutmut_1: get(None) returns None → early return False.
        """
        hub = _make_hub()
        worker_ws = _make_async_ws()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers["b"] = "operator"  # keep state alive after disconnect

        result = await hub.disconnect_worker("w1")

        assert result is True, (
            "disconnect_worker must return True when a worker was connected — "
            "mutmut_1 corrupts the get() key so st is always None → returns False"
        )

    async def test_worker_ws_is_none_after_disconnect(self) -> None:
        """st.worker_ws must be None (not '') after disconnect.

        Kills mutmut_9: st.worker_ws = "" leaves a non-None falsy value.
        """
        hub = _make_hub()
        worker_ws = _make_async_ws()
        browser_ws = _make_async_ws()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[browser_ws] = "operator"

        await hub.disconnect_worker("w1")

        async with hub._lock:
            st = hub._workers.get("w1")
            assert st is not None  # still has browser
            assert st.worker_ws is None, (
                "worker_ws must be None after disconnect, not '' — mutmut_9 sets worker_ws = ''"
            )

    async def test_hijack_owner_is_none_after_disconnect(self) -> None:
        """st.hijack_owner must be None (not '') after disconnect.

        Kills mutmut_15: st.hijack_owner = "" leaves a truthy-ish falsy value.
        """
        hub = _make_hub()
        worker_ws = _make_async_ws()
        browser_ws = _make_async_ws()
        hijack_ws = MagicMock()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[browser_ws] = "admin"
            st.hijack_owner = hijack_ws
            st.hijack_owner_expires_at = time.time() + 3600

        await hub.disconnect_worker("w1")

        async with hub._lock:
            st = hub._workers.get("w1")
            assert st is not None
            assert st.hijack_owner is None, (
                "hijack_owner must be None after disconnect, not '' — mutmut_15 sets hijack_owner = ''"
            )

    async def test_hijack_owner_expires_at_is_none_after_disconnect(self) -> None:
        """st.hijack_owner_expires_at must be None (not '') after disconnect.

        Kills mutmut_16: st.hijack_owner_expires_at = "" leaves a falsy string.
        """
        hub = _make_hub()
        worker_ws = _make_async_ws()
        browser_ws = _make_async_ws()
        hijack_ws = MagicMock()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[browser_ws] = "admin"
            st.hijack_owner = hijack_ws
            st.hijack_owner_expires_at = time.time() + 3600

        await hub.disconnect_worker("w1")

        async with hub._lock:
            st = hub._workers.get("w1")
            assert st is not None
            assert st.hijack_owner_expires_at is None, (
                "hijack_owner_expires_at must be None after disconnect, not '' — "
                "mutmut_16 sets hijack_owner_expires_at = ''"
            )

    async def test_returns_false_when_no_worker(self) -> None:
        """disconnect_worker returns False when no worker is connected."""
        hub = _make_hub()
        result = await hub.disconnect_worker("no-such-worker")
        assert result is False
