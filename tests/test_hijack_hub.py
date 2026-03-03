#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Integration tests for TermHub logic (direct method calls, no HTTP)."""

from __future__ import annotations

import asyncio
import json
import re
import time
from unittest.mock import AsyncMock

from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.models import BotTermState, HijackSession

# ---------------------------------------------------------------------------
# State creation
# ---------------------------------------------------------------------------


async def test_get_creates_state() -> None:
    hub = TermHub()
    st = await hub._get("bot1")
    assert isinstance(st, BotTermState)
    assert "bot1" in hub._bots


async def test_get_returns_same_state() -> None:
    hub = TermHub()
    st1 = await hub._get("bot1")
    st2 = await hub._get("bot1")
    assert st1 is st2


async def test_is_hijacked_false_initially() -> None:
    hub = TermHub()
    st = await hub._get("bot1")
    assert not hub._is_hijacked(st)


# ---------------------------------------------------------------------------
# Cleanup — expired REST session
# ---------------------------------------------------------------------------


async def test_cleanup_expired_rest_session() -> None:
    hub = TermHub()
    await hub._get("bot1")
    hub._bots["bot1"].hijack_session = HijackSession(
        hijack_id="abc",
        owner="test",
        acquired_at=time.time() - 200,
        lease_expires_at=time.time() - 1,
        last_heartbeat=time.time() - 200,
    )
    expired = await hub._cleanup_expired_hijack("bot1")
    assert expired
    # Bot is fully idle after session expiry → pruned from _bots.
    assert "bot1" not in hub._bots


async def test_cleanup_not_expired_rest_session() -> None:
    hub = TermHub()
    await hub._get("bot1")
    hub._bots["bot1"].hijack_session = HijackSession(
        hijack_id="abc",
        owner="test",
        acquired_at=time.time(),
        lease_expires_at=time.time() + 3600,
        last_heartbeat=time.time(),
    )
    expired = await hub._cleanup_expired_hijack("bot1")
    assert not expired
    assert hub._bots["bot1"].hijack_session is not None


# ---------------------------------------------------------------------------
# Cleanup — expired dashboard owner
# ---------------------------------------------------------------------------


async def test_cleanup_expired_dashboard_owner() -> None:
    hub = TermHub()
    await hub._get("bot1")
    mock_ws = AsyncMock()
    hub._bots["bot1"].hijack_owner = mock_ws
    hub._bots["bot1"].hijack_owner_expires_at = time.time() - 1
    expired = await hub._cleanup_expired_hijack("bot1")
    assert expired
    # Bot is fully idle after owner expiry → pruned from _bots.
    assert "bot1" not in hub._bots


async def test_cleanup_missing_bot_returns_false() -> None:
    hub = TermHub()
    result = await hub._cleanup_expired_hijack("nonexistent")
    assert result is False


# ---------------------------------------------------------------------------
# Cleanup — resume message sent to worker
# ---------------------------------------------------------------------------


async def test_cleanup_sends_resume_to_worker() -> None:
    hub = TermHub()
    await hub._get("bot1")
    mock_ws = AsyncMock()
    hub._bots["bot1"].worker_ws = mock_ws
    hub._bots["bot1"].hijack_session = HijackSession(
        hijack_id="abc",
        owner="test",
        acquired_at=time.time() - 200,
        lease_expires_at=time.time() - 1,
        last_heartbeat=time.time() - 200,
    )
    await hub._cleanup_expired_hijack("bot1")
    mock_ws.send_text.assert_awaited_once()
    sent_msg = json.loads(mock_ws.send_text.await_args[0][0])
    assert sent_msg["action"] == "resume"


# ---------------------------------------------------------------------------
# Notify callbacks
# ---------------------------------------------------------------------------


async def test_notify_hijack_changed_sync() -> None:
    results: list[tuple] = []

    def cb(bot_id: str, enabled: bool, owner: str | None) -> None:
        results.append((bot_id, enabled, owner))

    hub = TermHub(on_hijack_changed=cb)
    hub._notify_hijack_changed("bot1", enabled=True, owner="me")
    assert results == [("bot1", True, "me")]


async def test_notify_hijack_changed_async() -> None:
    results: list[tuple] = []

    async def cb(bot_id: str, enabled: bool, owner: str | None) -> None:
        results.append((bot_id, enabled, owner))

    hub = TermHub(on_hijack_changed=cb)
    hub._notify_hijack_changed("bot1", enabled=True, owner="me")
    await asyncio.sleep(0)  # allow the scheduled task to run
    assert results == [("bot1", True, "me")]


async def test_notify_no_callback() -> None:
    hub = TermHub()
    # Should not raise
    hub._notify_hijack_changed("bot1", enabled=True, owner=None)


# ---------------------------------------------------------------------------
# _snapshot_matches
# ---------------------------------------------------------------------------


async def test_snapshot_matches_prompt_id() -> None:
    snapshot = {"prompt_detected": {"prompt_id": "main_menu"}, "screen": "hello"}
    assert TermHub._snapshot_matches(snapshot, expect_prompt_id="main_menu", expect_regex=None)


async def test_snapshot_matches_regex() -> None:
    snapshot = {"screen": "Welcome to the server"}
    pattern = re.compile("Welcome", re.IGNORECASE)
    assert TermHub._snapshot_matches(snapshot, expect_prompt_id=None, expect_regex=pattern)


async def test_snapshot_not_matches_wrong_prompt_id() -> None:
    snapshot = {"prompt_detected": {"prompt_id": "main_menu"}, "screen": "hello"}
    assert not TermHub._snapshot_matches(snapshot, expect_prompt_id="other", expect_regex=None)


async def test_snapshot_not_matches_none() -> None:
    assert not TermHub._snapshot_matches(None, expect_prompt_id="main_menu", expect_regex=None)


async def test_snapshot_matches_no_constraints() -> None:
    # Both constraints None → always True when snapshot is non-None
    snapshot = {"screen": "anything"}
    assert TermHub._snapshot_matches(snapshot, expect_prompt_id=None, expect_regex=None)


# ---------------------------------------------------------------------------
# _clamp_lease
# ---------------------------------------------------------------------------


async def test_clamp_lease_minimum() -> None:
    assert TermHub._clamp_lease(0) == 1


async def test_clamp_lease_normal() -> None:
    assert TermHub._clamp_lease(300) == 300


async def test_clamp_lease_maximum() -> None:
    assert TermHub._clamp_lease(9999) == 3600


# ---------------------------------------------------------------------------
# _is_dashboard_hijack_active
# ---------------------------------------------------------------------------


async def test_is_dashboard_hijack_active_expired() -> None:
    st = BotTermState()
    st.hijack_owner = AsyncMock()
    st.hijack_owner_expires_at = time.time() - 1
    assert not TermHub._is_dashboard_hijack_active(st)


async def test_is_dashboard_hijack_active_no_expiry() -> None:
    st = BotTermState()
    st.hijack_owner = AsyncMock()
    st.hijack_owner_expires_at = None  # no expiry → permanent
    assert TermHub._is_dashboard_hijack_active(st)


async def test_is_dashboard_hijack_active_future() -> None:
    st = BotTermState()
    st.hijack_owner = AsyncMock()
    st.hijack_owner_expires_at = time.time() + 3600
    assert TermHub._is_dashboard_hijack_active(st)


async def test_is_dashboard_hijack_active_no_owner() -> None:
    st = BotTermState()
    assert not TermHub._is_dashboard_hijack_active(st)


async def test_wait_for_guard_invalid_regex() -> None:
    hub = TermHub()
    ok, snapshot, reason = await hub._wait_for_guard(
        "bot1",
        expect_prompt_id=None,
        expect_regex="[invalid",
        timeout_ms=100,
        poll_interval_ms=50,
    )
    assert not ok
    assert reason is not None
    assert "invalid" in reason


async def test_wait_for_guard_no_constraints_returns_immediately() -> None:
    hub = TermHub()
    await hub._get("bot1")
    hub._bots["bot1"].last_snapshot = {"screen": "hello"}
    ok, snapshot, reason = await hub._wait_for_guard(
        "bot1",
        expect_prompt_id=None,
        expect_regex=None,
        timeout_ms=100,
        poll_interval_ms=50,
    )
    assert ok
    assert reason is None


async def test_wait_for_guard_with_prompt_constraint() -> None:
    hub = TermHub()
    await hub._get("bot1")
    hub._bots["bot1"].last_snapshot = {
        "screen": "hello",
        "prompt_detected": {"prompt_id": "main_menu"},
    }
    ok, snapshot, reason = await hub._wait_for_guard(
        "bot1",
        expect_prompt_id="main_menu",
        expect_regex=None,
        timeout_ms=200,
        poll_interval_ms=20,
    )
    assert ok


async def test_wait_for_guard_timeout() -> None:
    hub = TermHub()
    # No snapshot → guard never satisfied
    ok, snapshot, reason = await hub._wait_for_guard(
        "bot1",
        expect_prompt_id="never",
        expect_regex=None,
        timeout_ms=50,
        poll_interval_ms=10,
    )
    assert not ok
    assert reason == "prompt_guard_not_satisfied"


async def test_broadcast_removes_dead_socket() -> None:
    hub = TermHub()
    await hub._get("bot1")

    dead_ws = AsyncMock()
    dead_ws.send_text = AsyncMock(side_effect=RuntimeError("disconnected"))
    hub._bots["bot1"].browsers.add(dead_ws)

    await hub._broadcast("bot1", {"type": "test"})
    # Dead socket should be removed
    assert dead_ws not in hub._bots["bot1"].browsers


async def test_broadcast_hijack_state_rest_session_active() -> None:
    """_broadcast_hijack_state uses hijack_session.lease_expires_at when REST session active."""
    hub = TermHub()
    await hub._get("bot1")
    mock_ws = AsyncMock()
    hub._bots["bot1"].browsers.add(mock_ws)
    hub._bots["bot1"].hijack_session = HijackSession(
        hijack_id="abc",
        owner="test",
        acquired_at=time.time(),
        lease_expires_at=time.time() + 3600,
        last_heartbeat=time.time(),
    )
    await hub._broadcast_hijack_state("bot1")
    mock_ws.send_text.assert_awaited_once()
    msg = json.loads(mock_ws.send_text.await_args[0][0])
    assert msg["hijacked"] is True


async def test_broadcast_hijack_state_removes_dead_socket() -> None:
    hub = TermHub()
    await hub._get("bot1")
    dead_ws = AsyncMock()
    dead_ws.send_text = AsyncMock(side_effect=RuntimeError("gone"))
    hub._bots["bot1"].browsers.add(dead_ws)

    await hub._broadcast_hijack_state("bot1")
    assert dead_ws not in hub._bots["bot1"].browsers


async def test_touch_hijack_owner_returns_none_when_no_bot() -> None:
    hub = TermHub()
    result = await hub._touch_hijack_owner("nonexistent")
    assert result is None


async def test_create_router_returns_router() -> None:
    from fastapi import APIRouter
    hub = TermHub()
    router = hub.create_router()
    assert router is not None


async def test_wait_for_snapshot_returns_immediately_if_available() -> None:
    """_wait_for_snapshot returns early when last_snapshot is already set (line 177)."""
    hub = TermHub()
    await hub._get("bot1")
    hub._bots["bot1"].last_snapshot = {"screen": "cached", "cols": 80, "rows": 25}
    result = await hub._wait_for_snapshot("bot1", timeout_ms=50)
    assert result is not None
    assert result["screen"] == "cached"


async def test_hijack_state_msg_owner_is_me() -> None:
    """_hijack_state_msg_for returns owner='me' when the calling ws is the hijack owner (line 311)."""
    hub = TermHub()
    await hub._get("bot1")
    mock_ws = AsyncMock()
    hub._bots["bot1"].hijack_owner = mock_ws
    hub._bots["bot1"].hijack_owner_expires_at = time.time() + 3600
    msg = await hub._hijack_state_msg_for("bot1", mock_ws)
    assert msg["owner"] == "me"


async def test_cleanup_expired_both_expired_sends_resume() -> None:
    """When both dashboard and REST expire, resume is sent once and owner_expired event is appended."""
    hub = TermHub()
    await hub._get("bot1")
    mock_ws = AsyncMock()
    hub._bots["bot1"].worker_ws = mock_ws
    hub._bots["bot1"].hijack_owner = AsyncMock()
    hub._bots["bot1"].hijack_owner_expires_at = time.time() - 1
    hub._bots["bot1"].hijack_session = HijackSession(
        hijack_id="abc",
        owner="test",
        acquired_at=time.time() - 200,
        lease_expires_at=time.time() - 1,
        last_heartbeat=time.time() - 200,
    )
    expired = await hub._cleanup_expired_hijack("bot1")
    assert expired
    mock_ws.send_text.assert_awaited()


# ---------------------------------------------------------------------------
# Fix 2 regression — _broadcast_hijack_state snapshots state under lock
# ---------------------------------------------------------------------------


async def test_broadcast_hijack_state_owner_gets_me_others_get_other() -> None:
    """Regression fix 2: hijack owner WebSocket receives owner='me'; other browsers get 'other'."""
    hub = TermHub()
    ws_owner = AsyncMock()
    ws_other = AsyncMock()

    async with hub._lock:
        st = hub._bots.setdefault("bot1", BotTermState())
        st.browsers = {ws_owner, ws_other}
        st.hijack_owner = ws_owner
        st.hijack_owner_expires_at = time.time() + 60

    await hub._broadcast_hijack_state("bot1")

    owner_msg = json.loads(ws_owner.send_text.await_args[0][0])
    assert owner_msg["owner"] == "me"
    assert owner_msg["type"] == "hijack_state"
    assert owner_msg["hijacked"] is True

    other_msg = json.loads(ws_other.send_text.await_args[0][0])
    assert other_msg["owner"] == "other"
    assert other_msg["hijacked"] is True


async def test_broadcast_hijack_state_no_hijack_owner_is_none() -> None:
    """Regression fix 2: when not hijacked, owner is None for all browsers."""
    hub = TermHub()
    ws1 = AsyncMock()
    ws2 = AsyncMock()

    async with hub._lock:
        st = hub._bots.setdefault("bot1", BotTermState())
        st.browsers = {ws1, ws2}

    await hub._broadcast_hijack_state("bot1")

    msg1 = json.loads(ws1.send_text.await_args[0][0])
    msg2 = json.loads(ws2.send_text.await_args[0][0])
    assert msg1["owner"] is None
    assert msg2["owner"] is None
    assert msg1["hijacked"] is False


# ---------------------------------------------------------------------------
# Fix 4 regression — _notify_hijack_changed done_callback logs exceptions
# ---------------------------------------------------------------------------


async def test_notify_hijack_changed_async_exception_is_logged(caplog) -> None:
    """Regression fix 4: exceptions from async on_hijack_changed are logged, not silently dropped."""
    import logging

    async def failing_cb(bot_id: str, enabled: bool, owner: str | None) -> None:
        raise ValueError("callback error")

    hub = TermHub(on_hijack_changed=failing_cb)

    with caplog.at_level(logging.WARNING, logger="provide.terminal.hijack.hub"):
        hub._notify_hijack_changed("bot1", enabled=True, owner="me")
        await asyncio.sleep(0.05)  # let the fire-and-forget task run

    assert any("on_hijack_changed" in r.message for r in caplog.records), (
        "expected warning log for failed on_hijack_changed callback"
    )


async def test_notify_hijack_changed_successful_async_does_not_log(caplog) -> None:
    """Regression fix 4: a successful async callback produces no warning logs."""
    import logging

    async def ok_cb(bot_id: str, enabled: bool, owner: str | None) -> None:
        pass  # no exception

    hub = TermHub(on_hijack_changed=ok_cb)

    with caplog.at_level(logging.WARNING, logger="provide.terminal.hijack.hub"):
        hub._notify_hijack_changed("bot1", enabled=True, owner="me")
        await asyncio.sleep(0.05)

    assert not any("on_hijack_changed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Fix 3 regression — _try_release_ws_hijack atomic check-and-clear
# ---------------------------------------------------------------------------


async def test_try_release_ws_hijack_clears_owner() -> None:
    """Regression fix 3: _try_release_ws_hijack returns (True, False) and clears owner for the active owner."""
    hub = TermHub()
    ws = AsyncMock()
    async with hub._lock:
        st = hub._bots.setdefault("bot1", BotTermState())
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.time() + 60

    released, rest_active = await hub._try_release_ws_hijack("bot1", ws)
    assert released is True
    assert rest_active is False
    assert hub._bots["bot1"].hijack_owner is None
    assert hub._bots["bot1"].hijack_owner_expires_at is None


async def test_try_release_ws_hijack_rejects_non_owner() -> None:
    """Regression fix 3: _try_release_ws_hijack returns (False, ...) and leaves owner intact for a non-owner ws."""
    hub = TermHub()
    ws_owner = AsyncMock()
    ws_other = AsyncMock()
    async with hub._lock:
        st = hub._bots.setdefault("bot1", BotTermState())
        st.hijack_owner = ws_owner
        st.hijack_owner_expires_at = time.time() + 60

    released, rest_active = await hub._try_release_ws_hijack("bot1", ws_other)
    assert released is False
    # Owner must be untouched
    assert hub._bots["bot1"].hijack_owner is ws_owner


async def test_try_release_ws_hijack_noop_when_no_bot() -> None:
    """Regression fix 3: _try_release_ws_hijack returns (False, False) gracefully for an unknown bot_id."""
    hub = TermHub()
    ws = AsyncMock()
    released, rest_active = await hub._try_release_ws_hijack("nonexistent", ws)
    assert released is False
    assert rest_active is False


async def test_try_release_ws_hijack_noop_when_expired() -> None:
    """Regression fix 3: _try_release_ws_hijack returns (False, ...) when the lease has already expired."""
    hub = TermHub()
    ws = AsyncMock()
    async with hub._lock:
        st = hub._bots.setdefault("bot1", BotTermState())
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.time() - 1  # already expired

    released, _rest = await hub._try_release_ws_hijack("bot1", ws)
    assert released is False


# ---------------------------------------------------------------------------
# Fix 4 regression — _is_owner reads under the lock
# ---------------------------------------------------------------------------


async def test_is_owner_returns_false_when_no_bot() -> None:
    """Regression fix 4: _is_owner returns False for an unknown bot_id (lock-based read)."""
    hub = TermHub()
    ws = AsyncMock()
    result = await hub._is_owner("nonexistent", ws)
    assert result is False


async def test_is_owner_returns_true_for_active_owner() -> None:
    """Regression fix 4: _is_owner returns True for the active owner under lock."""
    hub = TermHub()
    ws = AsyncMock()
    async with hub._lock:
        st = hub._bots.setdefault("bot1", BotTermState())
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.time() + 60

    assert await hub._is_owner("bot1", ws) is True


async def test_is_owner_returns_false_for_different_ws() -> None:
    """Regression fix 4: _is_owner returns False when the ws is not the owner (identity check under lock)."""
    hub = TermHub()
    ws_owner = AsyncMock()
    ws_other = AsyncMock()
    async with hub._lock:
        st = hub._bots.setdefault("bot1", BotTermState())
        st.hijack_owner = ws_owner
        st.hijack_owner_expires_at = time.time() + 60

    assert await hub._is_owner("bot1", ws_other) is False


async def test_is_owner_returns_false_after_owner_cleared() -> None:
    """Regression fix 4: _is_owner reflects cleared state immediately (lock ensures fresh read)."""
    hub = TermHub()
    ws = AsyncMock()
    async with hub._lock:
        st = hub._bots.setdefault("bot1", BotTermState())
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.time() + 60

    assert await hub._is_owner("bot1", ws) is True

    # Clear owner atomically (simulate another coroutine releasing)
    async with hub._lock:
        hub._bots["bot1"].hijack_owner = None
        hub._bots["bot1"].hijack_owner_expires_at = None

    assert await hub._is_owner("bot1", ws) is False


# ---------------------------------------------------------------------------
# Round-6 regression — _touch_if_owner atomic check+extend
# ---------------------------------------------------------------------------


async def test_touch_if_owner_returns_expiry_for_active_owner() -> None:
    """Round-6 fix: _touch_if_owner returns new lease_expires_at when ws is active owner."""
    hub = TermHub()
    ws = AsyncMock()
    async with hub._lock:
        st = hub._bots.setdefault("bot1", BotTermState())
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.time() + 60

    result = await hub._touch_if_owner("bot1", ws)
    assert result is not None
    assert result > time.time()


async def test_touch_if_owner_returns_none_for_non_owner() -> None:
    """Round-6 fix: _touch_if_owner returns None when ws is not the owner."""
    hub = TermHub()
    ws_owner = AsyncMock()
    ws_other = AsyncMock()
    async with hub._lock:
        st = hub._bots.setdefault("bot1", BotTermState())
        st.hijack_owner = ws_owner
        st.hijack_owner_expires_at = time.time() + 60

    result = await hub._touch_if_owner("bot1", ws_other)
    assert result is None


async def test_touch_if_owner_returns_none_when_no_bot() -> None:
    """Round-6 fix: _touch_if_owner returns None for an unknown bot_id."""
    hub = TermHub()
    ws = AsyncMock()
    result = await hub._touch_if_owner("nonexistent", ws)
    assert result is None


async def test_touch_if_owner_returns_none_after_owner_cleared() -> None:
    """Round-6 fix: _touch_if_owner returns None after owner has been cleared."""
    hub = TermHub()
    ws = AsyncMock()
    async with hub._lock:
        st = hub._bots.setdefault("bot1", BotTermState())
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.time() + 60

    assert await hub._touch_if_owner("bot1", ws) is not None

    async with hub._lock:
        hub._bots["bot1"].hijack_owner = None

    assert await hub._touch_if_owner("bot1", ws) is None


# ---------------------------------------------------------------------------
# Round-6 regression — _get_rest_session reads hijack_session under lock
# ---------------------------------------------------------------------------


async def test_get_rest_session_returns_session_when_valid() -> None:
    """Round-6 fix: _get_rest_session returns the session for a valid hijack_id."""
    hub = TermHub()
    async with hub._lock:
        st = hub._bots.setdefault("bot1", BotTermState())
        st.hijack_session = HijackSession(
            hijack_id="abc123",
            owner="test",
            acquired_at=time.time(),
            lease_expires_at=time.time() + 3600,
            last_heartbeat=time.time(),
        )

    result = await hub._get_rest_session("bot1", "abc123")
    assert result is not None
    assert result.hijack_id == "abc123"


async def test_get_rest_session_returns_none_for_wrong_hijack_id() -> None:
    """Round-6 fix: _get_rest_session returns None when hijack_id doesn't match (checked under lock)."""
    hub = TermHub()
    async with hub._lock:
        st = hub._bots.setdefault("bot1", BotTermState())
        st.hijack_session = HijackSession(
            hijack_id="abc123",
            owner="test",
            acquired_at=time.time(),
            lease_expires_at=time.time() + 3600,
            last_heartbeat=time.time(),
        )

    result = await hub._get_rest_session("bot1", "wrong-id")
    assert result is None


async def test_get_rest_session_returns_none_for_missing_bot() -> None:
    """Round-6 fix: _get_rest_session returns None for an unknown bot_id (lock-safe)."""
    hub = TermHub()
    result = await hub._get_rest_session("nonexistent", "any-id")
    assert result is None


# ---------------------------------------------------------------------------
# Round-6 regression — _hijack_state_msg_for snapshots all fields under lock
# ---------------------------------------------------------------------------


async def test_hijack_state_msg_for_no_bot_returns_not_hijacked() -> None:
    """Round-6 fix: _hijack_state_msg_for returns unhijacked state for an unknown bot_id."""
    hub = TermHub()
    ws = AsyncMock()
    msg = await hub._hijack_state_msg_for("nonexistent", ws)
    assert msg["hijacked"] is False
    assert msg["owner"] is None


async def test_hijack_state_msg_for_rest_session_returns_other() -> None:
    """Round-6 fix: _hijack_state_msg_for returns owner='other' for a non-owner ws when REST active."""
    hub = TermHub()
    ws = AsyncMock()
    async with hub._lock:
        st = hub._bots.setdefault("bot1", BotTermState())
        st.hijack_session = HijackSession(
            hijack_id="s1",
            owner="tester",
            acquired_at=time.time(),
            lease_expires_at=time.time() + 3600,
            last_heartbeat=time.time(),
        )

    msg = await hub._hijack_state_msg_for("bot1", ws)
    assert msg["hijacked"] is True
    assert msg["owner"] == "other"


async def test_try_release_ws_hijack_returns_rest_active_when_rest_session_present() -> None:
    """Round-6 fix: _try_release_ws_hijack returns rest_active=True when REST session active post-release."""
    hub = TermHub()
    ws = AsyncMock()
    async with hub._lock:
        st = hub._bots.setdefault("bot1", BotTermState())
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.time() + 60
        st.hijack_session = HijackSession(
            hijack_id="r1",
            owner="rest-client",
            acquired_at=time.time(),
            lease_expires_at=time.time() + 3600,
            last_heartbeat=time.time(),
        )

    released, rest_active = await hub._try_release_ws_hijack("bot1", ws)
    assert released is True
    assert rest_active is True  # REST session still active after dashboard WS released


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
        st = hub._bots.setdefault("bot1", BotTermState())
        st.browsers.add(ws1)
        st.browsers.add(ws2)

    # Make ws1.send_text remove ws2 from the live set mid-iteration to simulate
    # a concurrent disconnect happening between sends.
    async def _send_and_remove(payload: str) -> None:
        async with hub._lock:
            st2 = hub._bots.get("bot1")
            if st2 is not None:
                st2.browsers.discard(ws2)

    ws1.send_text = AsyncMock(side_effect=_send_and_remove)

    # Must not raise RuntimeError about set-changed-size during iteration.
    await hub._broadcast("bot1", {"type": "ping"})


# ---------------------------------------------------------------------------
# _prune_if_idle
# ---------------------------------------------------------------------------


async def test_prune_if_idle_removes_fully_disconnected_bot() -> None:
    """A bot with no worker, no browsers, and no hijack is removed from _bots."""
    hub = TermHub()
    await hub._get("bot1")
    assert "bot1" in hub._bots

    await hub._prune_if_idle("bot1")

    assert "bot1" not in hub._bots


async def test_prune_if_idle_keeps_bot_with_active_worker() -> None:
    hub = TermHub()
    async with hub._lock:
        st = hub._bots.setdefault("bot1", BotTermState())
        st.worker_ws = AsyncMock()

    await hub._prune_if_idle("bot1")

    assert "bot1" in hub._bots


async def test_prune_if_idle_keeps_bot_with_browser() -> None:
    hub = TermHub()
    async with hub._lock:
        st = hub._bots.setdefault("bot1", BotTermState())
        st.browsers.add(AsyncMock())

    await hub._prune_if_idle("bot1")

    assert "bot1" in hub._bots


async def test_prune_if_idle_keeps_bot_with_active_rest_session() -> None:
    import time

    hub = TermHub()
    async with hub._lock:
        st = hub._bots.setdefault("bot1", BotTermState())
        st.hijack_session = HijackSession(
            hijack_id="x",
            owner="test",
            acquired_at=time.time(),
            lease_expires_at=time.time() + 90,
            last_heartbeat=time.time(),
        )

    await hub._prune_if_idle("bot1")

    assert "bot1" in hub._bots


async def test_prune_if_idle_keeps_bot_with_dashboard_owner() -> None:
    hub = TermHub()
    async with hub._lock:
        st = hub._bots.setdefault("bot1", BotTermState())
        st.hijack_owner = AsyncMock()

    await hub._prune_if_idle("bot1")

    assert "bot1" in hub._bots


async def test_prune_if_idle_noop_for_unknown_bot() -> None:
    """Calling _prune_if_idle for a bot that doesn't exist is a no-op."""
    hub = TermHub()
    await hub._prune_if_idle("no-such-bot")  # must not raise
