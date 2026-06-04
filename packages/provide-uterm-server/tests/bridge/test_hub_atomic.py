#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Atomic/TOCTOU regression tests and prune tests for TermHub.

Split from test_hijack_hub.py to keep files under 500 LOC.
Covers: _try_release_ws_hijack, _is_owner, _touch_if_owner,
_get_rest_session, _hijack_state_msg_for, _broadcast set-snapshot,
and _prune_if_idle.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

# ---------------------------------------------------------------------------
# Fix 3 regression — _try_release_ws_hijack atomic check-and-clear
# ---------------------------------------------------------------------------


async def test_try_release_ws_hijack_clears_owner() -> None:
    """Regression fix 3: _try_release_ws_hijack returns (True, False) and clears owner for the active owner."""
    hub = TermHub()
    ws = AsyncMock()
    async with hub._lock:
        st = hub.registry._workers.setdefault("bot1", WorkerTermState())
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 60

    released, rest_active = await hub.try_release_ws_hijack("bot1", ws)
    assert released is True
    assert rest_active is False
    assert hub.registry._workers["bot1"].hijack_owner is None
    assert hub.registry._workers["bot1"].hijack_owner_expires_at is None


async def test_try_release_ws_hijack_rejects_non_owner() -> None:
    """Regression fix 3: _try_release_ws_hijack returns (False, ...) and leaves owner intact for a non-owner ws."""
    hub = TermHub()
    ws_owner = AsyncMock()
    ws_other = AsyncMock()
    async with hub._lock:
        st = hub.registry._workers.setdefault("bot1", WorkerTermState())
        st.hijack_owner = ws_owner
        st.hijack_owner_expires_at = time.monotonic() + 60

    released, rest_active = await hub.try_release_ws_hijack("bot1", ws_other)
    assert released is False
    # Owner must be untouched
    assert hub.registry._workers["bot1"].hijack_owner is ws_owner


async def test_try_release_ws_hijack_noop_when_no_bot() -> None:
    """Regression fix 3: _try_release_ws_hijack returns (False, False) gracefully for an unknown bot_id."""
    hub = TermHub()
    ws = AsyncMock()
    released, rest_active = await hub.try_release_ws_hijack("nonexistent", ws)
    assert released is False
    assert rest_active is False


async def test_try_release_ws_hijack_noop_when_expired() -> None:
    """Regression fix 3: _try_release_ws_hijack returns (False, ...) when the lease has already expired."""
    hub = TermHub()
    ws = AsyncMock()
    async with hub._lock:
        st = hub.registry._workers.setdefault("bot1", WorkerTermState())
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() - 1  # already expired

    released, _rest = await hub.try_release_ws_hijack("bot1", ws)
    assert released is False


# ---------------------------------------------------------------------------
# Round-6 regression — _touch_if_owner atomic check+extend
# ---------------------------------------------------------------------------


async def test_touch_if_owner_returns_expiry_for_active_owner() -> None:
    """Round-6 fix: _touch_if_owner returns new lease_expires_at when ws is active owner."""
    hub = TermHub()
    ws = AsyncMock()
    async with hub._lock:
        st = hub.registry._workers.setdefault("bot1", WorkerTermState())
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 60

    result = await hub.touch_if_owner("bot1", ws)
    assert result is not None
    assert result > time.monotonic()


async def test_touch_if_owner_returns_none_for_non_owner() -> None:
    """Round-6 fix: _touch_if_owner returns None when ws is not the owner."""
    hub = TermHub()
    ws_owner = AsyncMock()
    ws_other = AsyncMock()
    async with hub._lock:
        st = hub.registry._workers.setdefault("bot1", WorkerTermState())
        st.hijack_owner = ws_owner
        st.hijack_owner_expires_at = time.monotonic() + 60

    result = await hub.touch_if_owner("bot1", ws_other)
    assert result is None


async def test_touch_if_owner_returns_none_when_no_bot() -> None:
    """Round-6 fix: _touch_if_owner returns None for an unknown bot_id."""
    hub = TermHub()
    ws = AsyncMock()
    result = await hub.touch_if_owner("nonexistent", ws)
    assert result is None


async def test_touch_if_owner_returns_none_after_owner_cleared() -> None:
    """Round-6 fix: _touch_if_owner returns None after owner has been cleared."""
    hub = TermHub()
    ws = AsyncMock()
    async with hub._lock:
        st = hub.registry._workers.setdefault("bot1", WorkerTermState())
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 60

    assert await hub.touch_if_owner("bot1", ws) is not None

    async with hub._lock:
        hub.registry._workers["bot1"].hijack_owner = None

    assert await hub.touch_if_owner("bot1", ws) is None


# ---------------------------------------------------------------------------
# Round-6 regression — _get_rest_session reads hijack_session under lock
# ---------------------------------------------------------------------------


async def test_get_rest_session_returns_session_when_valid() -> None:
    """Round-6 fix: _get_rest_session returns the session for a valid hijack_id."""
    hub = TermHub()
    async with hub._lock:
        st = hub.registry._workers.setdefault("bot1", WorkerTermState())
        st.hijack_session = HijackSession(
            hijack_id="abc123",
            owner="test",
            acquired_at=time.monotonic(),
            lease_expires_at=time.monotonic() + 3600,
            last_heartbeat=time.monotonic(),
        )

    result = await hub.get_rest_session("bot1", "abc123")
    assert result is not None
    assert result.hijack_id == "abc123"


async def test_get_rest_session_returns_none_for_wrong_hijack_id() -> None:
    """Round-6 fix: _get_rest_session returns None when hijack_id doesn't match (checked under lock)."""
    hub = TermHub()
    async with hub._lock:
        st = hub.registry._workers.setdefault("bot1", WorkerTermState())
        st.hijack_session = HijackSession(
            hijack_id="abc123",
            owner="test",
            acquired_at=time.monotonic(),
            lease_expires_at=time.monotonic() + 3600,
            last_heartbeat=time.monotonic(),
        )

    result = await hub.get_rest_session("bot1", "wrong-id")
    assert result is None


async def test_get_rest_session_returns_none_for_missing_bot() -> None:
    """Round-6 fix: _get_rest_session returns None for an unknown bot_id (lock-safe)."""
    hub = TermHub()
    result = await hub.get_rest_session("nonexistent", "any-id")
    assert result is None


# ---------------------------------------------------------------------------
# Round-6 regression — _hijack_state_msg_for snapshots all fields under lock
# ---------------------------------------------------------------------------


async def test_hijack_state_msg_for_no_bot_returns_not_hijacked() -> None:
    """Round-6 fix: _hijack_state_msg_for returns unhijacked state for an unknown bot_id."""
    hub = TermHub()
    ws = AsyncMock()
    msg = await hub.hijack_state_msg_for("nonexistent", ws)
    assert msg["hijacked"] is False
    assert msg["owner"] is None


async def test_hijack_state_msg_for_rest_session_returns_other() -> None:
    """Round-6 fix: _hijack_state_msg_for returns owner='other' for a non-owner ws when REST active."""
    hub = TermHub()
    ws = AsyncMock()
    async with hub._lock:
        st = hub.registry._workers.setdefault("bot1", WorkerTermState())
        st.hijack_session = HijackSession(
            hijack_id="s1",
            owner="tester",
            acquired_at=time.monotonic(),
            lease_expires_at=time.monotonic() + 3600,
            last_heartbeat=time.monotonic(),
        )

    msg = await hub.hijack_state_msg_for("bot1", ws)
    assert msg["hijacked"] is True
    assert msg["owner"] == "other"


async def test_try_release_ws_hijack_returns_rest_active_when_rest_session_present() -> None:
    """Round-6 fix: _try_release_ws_hijack returns rest_active=True when REST session active post-release."""
    hub = TermHub()
    ws = AsyncMock()
    async with hub._lock:
        st = hub.registry._workers.setdefault("bot1", WorkerTermState())
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 60
        st.hijack_session = HijackSession(
            hijack_id="r1",
            owner="rest-client",
            acquired_at=time.monotonic(),
            lease_expires_at=time.monotonic() + 3600,
            last_heartbeat=time.monotonic(),
        )

    released, rest_active = await hub.try_release_ws_hijack("bot1", ws)
    assert released is True
    assert rest_active is True  # REST session still active after dashboard WS released


async def test_cleanup_expired_hijack_increments_metric_counter() -> None:
    """Expired REST/dashboard leases increment hijack_lease_expiries_total."""
    seen: dict[str, int] = {}

    def _metric(name: str, value: int) -> None:
        seen[name] = seen.get(name, 0) + int(value)

    hub = TermHub(on_metric=_metric)
    ws = AsyncMock()
    async with hub._lock:
        st = hub.registry._workers.setdefault("bot1", WorkerTermState())
        st.worker_ws = ws
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() - 1
        st.hijack_session = HijackSession(
            hijack_id="expired-1",
            owner="owner-a",
            acquired_at=time.monotonic() - 100,
            lease_expires_at=time.monotonic() - 1,
            last_heartbeat=time.monotonic() - 50,
        )

    changed = await hub.cleanup_expired_hijack("bot1")
    assert changed is True
    assert seen.get("hijack_lease_expiries_total", 0) >= 1


# ---------------------------------------------------------------------------
# Round-9 regression: _broadcast snapshots browsers under lock
# ---------------------------------------------------------------------------


async def test_broadcast_does_not_iterate_live_set() -> None:
    """Round-9 fix: _broadcast must snapshot st.browsers under the lock before
    iterating, so a concurrent disconnect that removes a WebSocket while we are
    sending does not raise RuntimeError('Set changed size during iteration').
    """
    hub = TermHub()
    ws1 = AsyncMock()
    ws2 = AsyncMock()

    async with hub._lock:
        st = hub.registry._workers.setdefault("bot1", WorkerTermState())
        st.browsers[ws1] = "operator"
        st.browsers[ws2] = "operator"

    # Make ws1.send_text remove ws2 from the live dict mid-iteration to simulate
    # a concurrent disconnect happening between sends.
    async def _send_and_remove(payload: str) -> None:
        async with hub._lock:
            st2 = hub.registry._workers.get("bot1")
            if st2 is not None:
                st2.browsers.pop(ws2, None)

    ws1.send_text = AsyncMock(side_effect=_send_and_remove)

    # Must not raise RuntimeError about set-changed-size during iteration.
    await hub.broadcast("bot1", {"type": "ping"})


# ---------------------------------------------------------------------------
# _prune_if_idle
# ---------------------------------------------------------------------------


async def test_prune_if_idle_removes_fully_disconnected_bot() -> None:
    """A bot with no worker, no browsers, and no hijack is removed from _bots."""
    hub = TermHub()
    await hub._get("bot1")
    assert "bot1" in hub.registry._workers

    await hub.prune_if_idle("bot1")

    assert "bot1" not in hub.registry._workers


async def test_prune_if_idle_keeps_bot_with_active_worker() -> None:
    hub = TermHub()
    async with hub._lock:
        st = hub.registry._workers.setdefault("bot1", WorkerTermState())
        st.worker_ws = AsyncMock()

    await hub.prune_if_idle("bot1")

    assert "bot1" in hub.registry._workers


async def test_prune_if_idle_keeps_bot_with_browser() -> None:
    hub = TermHub()
    async with hub._lock:
        st = hub.registry._workers.setdefault("bot1", WorkerTermState())
        st.browsers[AsyncMock()] = "operator"

    await hub.prune_if_idle("bot1")

    assert "bot1" in hub.registry._workers


async def test_prune_if_idle_keeps_bot_with_active_rest_session() -> None:
    hub = TermHub()
    async with hub._lock:
        st = hub.registry._workers.setdefault("bot1", WorkerTermState())
        st.hijack_session = HijackSession(
            hijack_id="x",
            owner="test",
            acquired_at=time.monotonic(),
            lease_expires_at=time.monotonic() + 90,
            last_heartbeat=time.monotonic(),
        )

    await hub.prune_if_idle("bot1")

    assert "bot1" in hub.registry._workers


async def test_prune_if_idle_keeps_bot_with_dashboard_owner() -> None:
    hub = TermHub()
    async with hub._lock:
        st = hub.registry._workers.setdefault("bot1", WorkerTermState())
        st.hijack_owner = AsyncMock()

    await hub.prune_if_idle("bot1")

    assert "bot1" in hub.registry._workers


async def test_prune_if_idle_noop_for_unknown_bot() -> None:
    """Calling _prune_if_idle for a bot that doesn't exist is a no-op."""
    hub = TermHub()
    await hub.prune_if_idle("no-such-bot")  # must not raise


# ---------------------------------------------------------------------------
# _append_event: pruned-worker guard
# ---------------------------------------------------------------------------


async def test_append_event_after_prune_does_not_resurrect_worker() -> None:
    """_append_event must drop the event rather than re-creating a ghost
    WorkerTermState when the worker has already been pruned.

    Regression guard: if the .get() is ever changed back to setdefault, a
    pruned worker would be resurrected with an active events deque but no
    connections or leases — an entry _prune_if_idle cannot later clean up
    until the next explicit prune call.
    """
    hub = TermHub()
    await hub._get("bot1")
    assert "bot1" in hub.registry._workers

    # Prune immediately (no connections, no leases)
    await hub.prune_if_idle("bot1")
    assert "bot1" not in hub.registry._workers, "worker should have been pruned"

    # Appending an event must not resurrect the entry
    evt = await hub.append_event("bot1", "test_event", {"x": 1})
    assert "bot1" not in hub.registry._workers, "worker must not be resurrected by _append_event"
    assert evt["seq"] == 0, "dropped event must return seq=0 sentinel"
