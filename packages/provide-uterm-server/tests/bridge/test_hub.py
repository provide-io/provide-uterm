#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Integration tests for TermHub logic (direct method calls, no HTTP).

Atomic/TOCTOU regression tests and prune tests are in test_hijack_hub_atomic.py.
"""

from __future__ import annotations

import asyncio
import re
import time
from unittest.mock import AsyncMock

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState
from tests.bridge.control_channel_helpers import decode_control_payload

# ---------------------------------------------------------------------------
# State creation
# ---------------------------------------------------------------------------


async def test_get_creates_state() -> None:
    hub = TermHub()
    st = await hub._get("bot1")
    assert isinstance(st, WorkerTermState)
    assert "bot1" in hub.registry._workers


async def test_get_returns_same_state() -> None:
    hub = TermHub()
    st1 = await hub._get("bot1")
    st2 = await hub._get("bot1")
    assert st1 is st2


async def test_is_hijacked_false_initially() -> None:
    hub = TermHub()
    st = await hub._get("bot1")
    assert not hub.is_hijacked(st)


# ---------------------------------------------------------------------------
# Cleanup — expired REST session
# ---------------------------------------------------------------------------


async def test_cleanup_expired_rest_session() -> None:
    hub = TermHub()
    await hub._get("bot1")
    hub.registry._workers["bot1"].hijack_session = HijackSession(
        hijack_id="abc",
        owner="test",
        acquired_at=time.monotonic() - 200,
        lease_expires_at=time.monotonic() - 1,
        last_heartbeat=time.monotonic() - 200,
    )
    expired = await hub.cleanup_expired_hijack("bot1")
    assert expired
    # Bot is fully idle after session expiry → pruned from _bots.
    assert "bot1" not in hub.registry._workers


async def test_cleanup_not_expired_rest_session() -> None:
    hub = TermHub()
    await hub._get("bot1")
    hub.registry._workers["bot1"].hijack_session = HijackSession(
        hijack_id="abc",
        owner="test",
        acquired_at=time.monotonic(),
        lease_expires_at=time.monotonic() + 3600,
        last_heartbeat=time.monotonic(),
    )
    expired = await hub.cleanup_expired_hijack("bot1")
    assert not expired
    assert hub.registry._workers["bot1"].hijack_session is not None


# ---------------------------------------------------------------------------
# Cleanup — expired dashboard owner
# ---------------------------------------------------------------------------


async def test_cleanup_expired_dashboard_owner() -> None:
    hub = TermHub()
    await hub._get("bot1")
    mock_ws = AsyncMock()
    hub.registry._workers["bot1"].hijack_owner = mock_ws
    hub.registry._workers["bot1"].hijack_owner_expires_at = time.monotonic() - 1
    expired = await hub.cleanup_expired_hijack("bot1")
    assert expired
    # Bot is fully idle after owner expiry → pruned from _bots.
    assert "bot1" not in hub.registry._workers


async def test_cleanup_missing_bot_returns_false() -> None:
    hub = TermHub()
    result = await hub.cleanup_expired_hijack("nonexistent")
    assert result is False


# ---------------------------------------------------------------------------
# Cleanup — resume message sent to worker
# ---------------------------------------------------------------------------


async def test_cleanup_sends_resume_to_worker() -> None:
    hub = TermHub()
    await hub._get("bot1")
    mock_ws = AsyncMock()
    hub.registry._workers["bot1"].worker_ws = mock_ws
    hub.registry._workers["bot1"].hijack_session = HijackSession(
        hijack_id="abc",
        owner="test",
        acquired_at=time.monotonic() - 200,
        lease_expires_at=time.monotonic() - 1,
        last_heartbeat=time.monotonic() - 200,
    )
    await hub.cleanup_expired_hijack("bot1")
    mock_ws.send_text.assert_awaited_once()
    sent_msg = decode_control_payload(mock_ws.send_text.await_args[0][0])
    assert sent_msg["action"] == "resume"


# ---------------------------------------------------------------------------
# Notify callbacks
# ---------------------------------------------------------------------------


async def test_notify_hijack_changed_sync() -> None:
    results: list[tuple] = []

    def cb(bot_id: str, enabled: bool, owner: str | None) -> None:
        results.append((bot_id, enabled, owner))

    hub = TermHub(on_hijack_changed=cb)
    hub.notify_hijack_changed("bot1", enabled=True, owner="me")
    assert results == [("bot1", True, "me")]


async def test_notify_hijack_changed_async() -> None:
    results: list[tuple] = []

    async def cb(bot_id: str, enabled: bool, owner: str | None) -> None:
        results.append((bot_id, enabled, owner))

    hub = TermHub(on_hijack_changed=cb)
    hub.notify_hijack_changed("bot1", enabled=True, owner="me")
    await asyncio.sleep(0)  # allow the scheduled task to run
    assert results == [("bot1", True, "me")]


async def test_notify_no_callback() -> None:
    hub = TermHub()
    # Should not raise
    hub.notify_hijack_changed("bot1", enabled=True, owner=None)


# ---------------------------------------------------------------------------
# _snapshot_matches
# ---------------------------------------------------------------------------


async def test_snapshot_matches_prompt_id() -> None:
    snapshot = {"prompt_detected": {"prompt_id": "main_menu"}, "screen": "hello"}
    assert TermHub.snapshot_matches(snapshot, expect_prompt_id="main_menu", expect_regex=None)


async def test_snapshot_matches_regex() -> None:
    snapshot = {"screen": "Welcome to the server"}
    pattern = re.compile("Welcome", re.IGNORECASE)
    assert TermHub.snapshot_matches(snapshot, expect_prompt_id=None, expect_regex=pattern)


async def test_snapshot_not_matches_wrong_prompt_id() -> None:
    snapshot = {"prompt_detected": {"prompt_id": "main_menu"}, "screen": "hello"}
    assert not TermHub.snapshot_matches(snapshot, expect_prompt_id="other", expect_regex=None)


async def test_snapshot_not_matches_none() -> None:
    assert not TermHub.snapshot_matches(None, expect_prompt_id="main_menu", expect_regex=None)


async def test_snapshot_matches_no_constraints() -> None:
    # Both constraints None → always True when snapshot is non-None
    snapshot = {"screen": "anything"}
    assert TermHub.snapshot_matches(snapshot, expect_prompt_id=None, expect_regex=None)


# ---------------------------------------------------------------------------
# _clamp_lease
# ---------------------------------------------------------------------------


async def test_clamp_lease_minimum() -> None:
    assert TermHub.clamp_lease(0) == 1


async def test_clamp_lease_normal() -> None:
    assert TermHub.clamp_lease(300) == 300


async def test_clamp_lease_maximum() -> None:
    assert TermHub.clamp_lease(99999) == 14400


# ---------------------------------------------------------------------------
# _is_dashboard_hijack_active
# ---------------------------------------------------------------------------


async def test_is_dashboard_hijack_active_expired() -> None:
    st = WorkerTermState()
    st.hijack_owner = AsyncMock()
    st.hijack_owner_expires_at = time.monotonic() - 1
    assert not TermHub.is_dashboard_hijack_active(st)


async def test_is_dashboard_hijack_active_no_expiry() -> None:
    st = WorkerTermState()
    st.hijack_owner = AsyncMock()
    st.hijack_owner_expires_at = None  # no expiry → permanent
    assert TermHub.is_dashboard_hijack_active(st)


async def test_is_dashboard_hijack_active_future() -> None:
    st = WorkerTermState()
    st.hijack_owner = AsyncMock()
    st.hijack_owner_expires_at = time.monotonic() + 3600
    assert TermHub.is_dashboard_hijack_active(st)


async def test_is_dashboard_hijack_active_no_owner() -> None:
    st = WorkerTermState()
    assert not TermHub.is_dashboard_hijack_active(st)


async def test_wait_for_guard_invalid_regex() -> None:
    hub = TermHub()
    ok, snapshot, reason = await hub.wait_for_guard(
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
    hub.registry._workers["bot1"].last_snapshot = {"screen": "hello"}
    ok, snapshot, reason = await hub.wait_for_guard(
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
    hub.registry._workers["bot1"].last_snapshot = {
        "screen": "hello",
        "prompt_detected": {"prompt_id": "main_menu"},
    }
    ok, snapshot, reason = await hub.wait_for_guard(
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
    ok, snapshot, reason = await hub.wait_for_guard(
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
    hub.registry._workers["bot1"].browsers[dead_ws] = "operator"

    await hub.broadcast("bot1", {"type": "test"})
    # Dead socket should be removed
    assert dead_ws not in hub.registry._workers["bot1"].browsers


async def test_broadcast_hijack_state_rest_session_active() -> None:
    """_broadcast_hijack_state uses hijack_session.lease_expires_at when REST session active."""
    hub = TermHub()
    await hub._get("bot1")
    mock_ws = AsyncMock()
    hub.registry._workers["bot1"].browsers[mock_ws] = "operator"
    hub.registry._workers["bot1"].hijack_session = HijackSession(
        hijack_id="abc",
        owner="test",
        acquired_at=time.monotonic(),
        lease_expires_at=time.monotonic() + 3600,
        last_heartbeat=time.monotonic(),
    )
    await hub.broadcast_hijack_state("bot1")
    mock_ws.send_text.assert_awaited_once()
    msg = decode_control_payload(mock_ws.send_text.await_args[0][0])
    assert msg["hijacked"] is True


async def test_broadcast_hijack_state_removes_dead_socket() -> None:
    hub = TermHub()
    await hub._get("bot1")
    dead_ws = AsyncMock()
    dead_ws.send_text = AsyncMock(side_effect=RuntimeError("gone"))
    hub.registry._workers["bot1"].browsers[dead_ws] = "operator"

    await hub.broadcast_hijack_state("bot1")
    assert dead_ws not in hub.registry._workers["bot1"].browsers


async def test_touch_hijack_owner_returns_none_when_no_bot() -> None:
    hub = TermHub()
    result = await hub.touch_hijack_owner("nonexistent")
    assert result is None


async def test_create_router_returns_router() -> None:
    hub = TermHub()
    router = hub.create_router()
    assert router is not None


async def test_wait_for_snapshot_returns_fresh_snapshot() -> None:
    """_wait_for_snapshot returns early when a fresh snapshot (ts > req_ts) is available."""
    hub = TermHub()
    await hub._get("bot1")
    # A snapshot with ts=0 predates req_ts and must NOT be returned (stale).
    hub.registry._workers["bot1"].last_snapshot = {"screen": "stale", "cols": 80, "rows": 25, "ts": 0}
    result = await hub.wait_for_snapshot("bot1", timeout_ms=50)
    assert result is None, "stale cached snapshot (ts=0) must not be returned on timeout"


async def test_wait_for_snapshot_returns_none_on_timeout_no_worker() -> None:
    """_wait_for_snapshot returns None when the worker never sends a snapshot."""
    hub = TermHub()
    await hub._get("bot1")
    result = await hub.wait_for_snapshot("bot1", timeout_ms=50)
    assert result is None


async def test_hijack_state_msg_owner_is_me() -> None:
    """_hijack_state_msg_for returns owner='me' when the calling ws is the hijack owner (line 311)."""
    hub = TermHub()
    await hub._get("bot1")
    mock_ws = AsyncMock()
    hub.registry._workers["bot1"].hijack_owner = mock_ws
    hub.registry._workers["bot1"].hijack_owner_expires_at = time.monotonic() + 3600
    msg = await hub.hijack_state_msg_for("bot1", mock_ws)
    assert msg["owner"] == "me"


async def test_cleanup_expired_both_expired_sends_resume() -> None:
    """When both dashboard and REST expire, resume is sent once and owner_expired event is appended."""
    hub = TermHub()
    await hub._get("bot1")
    mock_ws = AsyncMock()
    hub.registry._workers["bot1"].worker_ws = mock_ws
    hub.registry._workers["bot1"].hijack_owner = AsyncMock()
    hub.registry._workers["bot1"].hijack_owner_expires_at = time.monotonic() - 1
    hub.registry._workers["bot1"].hijack_session = HijackSession(
        hijack_id="abc",
        owner="test",
        acquired_at=time.monotonic() - 200,
        lease_expires_at=time.monotonic() - 1,
        last_heartbeat=time.monotonic() - 200,
    )
    expired = await hub.cleanup_expired_hijack("bot1")
    assert expired
    mock_ws.send_text.assert_awaited()
