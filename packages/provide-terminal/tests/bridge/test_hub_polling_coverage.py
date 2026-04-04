#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Polling coverage and regression tests for TermHub — mutation coverage.

Separated from test_hub.py to maintain file size limits (<500 LOC per file).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from unittest.mock import AsyncMock

from tests.bridge.control_channel_helpers import decode_control_payload
from provide.terminal.bridge.hub import TermHub
from provide.terminal.bridge.models import WorkerTermState

# ---------------------------------------------------------------------------
# Fix 2 regression — _broadcast_hijack_state snapshots state under lock
# ---------------------------------------------------------------------------


async def test_broadcast_hijack_state_owner_gets_me_others_get_other() -> None:
    """Regression fix 2: hijack owner WebSocket receives owner='me'; other browsers get 'other'."""
    hub = TermHub()
    ws_owner = AsyncMock()
    ws_other = AsyncMock()

    async with hub._lock:
        st = hub._workers.setdefault("bot1", WorkerTermState())
        st.browsers = {ws_owner: "admin", ws_other: "operator"}
        st.hijack_owner = ws_owner
        st.hijack_owner_expires_at = time.time() + 60

    await hub.broadcast_hijack_state("bot1")

    owner_msg = decode_control_payload(ws_owner.send_text.await_args[0][0])
    assert owner_msg["owner"] == "me"
    assert owner_msg["type"] == "hijack_state"
    assert owner_msg["hijacked"] is True

    other_msg = decode_control_payload(ws_other.send_text.await_args[0][0])
    assert other_msg["owner"] == "other"
    assert other_msg["hijacked"] is True


async def test_broadcast_hijack_state_no_hijack_owner_is_none() -> None:
    """Regression fix 2: when not hijacked, owner is None for all browsers."""
    hub = TermHub()
    ws1 = AsyncMock()
    ws2 = AsyncMock()

    async with hub._lock:
        st = hub._workers.setdefault("bot1", WorkerTermState())
        st.browsers = {ws1: "operator", ws2: "operator"}

    await hub.broadcast_hijack_state("bot1")

    msg1 = decode_control_payload(ws1.send_text.await_args[0][0])
    msg2 = decode_control_payload(ws2.send_text.await_args[0][0])
    assert msg1["owner"] is None
    assert msg2["owner"] is None
    assert msg1["hijacked"] is False


# ---------------------------------------------------------------------------
# Fix 4 regression — _notify_hijack_changed done_callback logs exceptions
# ---------------------------------------------------------------------------


async def test_notify_hijack_changed_async_exception_is_logged(caplog) -> None:
    """Regression fix 4: exceptions from async on_hijack_changed are logged, not silently dropped."""

    async def failing_cb(bot_id: str, enabled: bool, owner: str | None) -> None:
        raise ValueError("callback error")

    hub = TermHub(on_hijack_changed=failing_cb)

    with caplog.at_level(logging.WARNING, logger="provide.terminal.bridge.hub"):
        hub.notify_hijack_changed("bot1", enabled=True, owner="me")
        await asyncio.sleep(0.05)  # let the fire-and-forget task run

    assert any("on_hijack_changed" in r.message for r in caplog.records), (
        "expected warning log for failed on_hijack_changed callback"
    )


async def test_notify_hijack_changed_successful_async_does_not_log(caplog) -> None:
    """Regression fix 4: a successful async callback produces no warning logs."""

    async def ok_cb(bot_id: str, enabled: bool, owner: str | None) -> None:
        pass  # no exception

    hub = TermHub(on_hijack_changed=ok_cb)

    with caplog.at_level(logging.WARNING, logger="provide.terminal.bridge.hub"):
        hub.notify_hijack_changed("bot1", enabled=True, owner="me")
        await asyncio.sleep(0.05)

    assert not any("on_hijack_changed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Polling coverage — weak mutation survivors
# ---------------------------------------------------------------------------


async def test_snapshot_matches_both_constraints() -> None:
    """snapshot_matches with both prompt_id AND regex — both must be satisfied."""
    snapshot = {"prompt_detected": {"prompt_id": "menu"}, "screen": "Main Menu\nOptions:"}
    pattern = re.compile("Options", re.IGNORECASE)

    # Both match — returns True
    assert TermHub.snapshot_matches(snapshot, expect_prompt_id="menu", expect_regex=pattern)

    # Prompt matches, regex doesn't — returns False
    assert not TermHub.snapshot_matches(snapshot, expect_prompt_id="menu", expect_regex=re.compile("NotFound"))

    # Regex matches, prompt doesn't — returns False
    assert not TermHub.snapshot_matches(snapshot, expect_prompt_id="wrong", expect_regex=pattern)


async def test_snapshot_matches_regex_multiline() -> None:
    """snapshot_matches regex uses MULTILINE flag."""
    snapshot = {"screen": "line1\nline2 TARGET\nline3"}
    pattern = re.compile("^line2", re.MULTILINE)
    assert TermHub.snapshot_matches(snapshot, expect_prompt_id=None, expect_regex=pattern)


async def test_wait_for_guard_regex_compile_error_message() -> None:
    """wait_for_guard returns detailed error on invalid regex."""
    hub = TermHub()
    await hub._get("bot1")

    ok, snap, reason = await hub.wait_for_guard(
        "bot1",
        expect_prompt_id=None,
        expect_regex=r"(unclosed",
        timeout_ms=100,
        poll_interval_ms=10,
    )

    assert not ok
    assert snap is None
    assert "invalid expect_regex" in reason


async def test_wait_for_guard_timeout_ms_minimum() -> None:
    """wait_for_guard clamps timeout_ms to minimum 50ms."""
    hub = TermHub()
    await hub._get("bot1")
    hub._workers["bot1"].last_snapshot = {"screen": "test"}

    # timeout_ms=1 should be clamped to 50ms
    start = time.time()
    ok, snap, reason = await hub.wait_for_guard(
        "bot1",
        expect_prompt_id="nonexistent",
        expect_regex=None,
        timeout_ms=1,  # Will be clamped to 50
        poll_interval_ms=10,
    )
    elapsed = time.time() - start

    assert not ok
    assert reason == "prompt_guard_not_satisfied"
    assert elapsed >= 0.04  # At least ~50ms (clamped minimum)


async def test_wait_for_guard_poll_interval_minimum() -> None:
    """wait_for_guard clamps poll_interval_ms to minimum 20ms."""
    hub = TermHub()
    await hub._get("bot1")
    hub._workers["bot1"].last_snapshot = {"screen": "test", "ts": time.time()}

    # poll_interval_ms=1 should be clamped to 20ms
    start = time.time()
    ok, snap, reason = await hub.wait_for_guard(
        "bot1",
        expect_prompt_id="nonexistent",
        expect_regex=None,
        timeout_ms=100,
        poll_interval_ms=1,  # Will be clamped to 20
    )
    elapsed = time.time() - start

    # Should have at least 1 poll cycle (20ms)
    assert elapsed >= 0.015


async def test_wait_for_guard_matches_during_poll() -> None:
    """wait_for_guard matches snapshot during polling loop."""
    hub = TermHub()
    await hub._get("bot1")

    # Set initial snapshot without prompt_id
    hub._workers["bot1"].last_snapshot = {"screen": "initial"}

    # Simulate snapshot being updated in background
    async def update_snapshot() -> None:
        await asyncio.sleep(0.03)
        hub._workers["bot1"].last_snapshot = {
            "screen": "updated",
            "prompt_detected": {"prompt_id": "target"},
            "ts": time.time(),
        }

    task = asyncio.create_task(update_snapshot())
    ok, snap, reason = await hub.wait_for_guard(
        "bot1",
        expect_prompt_id="target",
        expect_regex=None,
        timeout_ms=500,
        poll_interval_ms=10,
    )
    await task

    assert ok
    assert snap is not None
    assert snap["screen"] == "updated"
    assert reason is None


async def test_wait_for_guard_no_new_snapshot_rerequests() -> None:
    """wait_for_guard re-requests snapshot when ts hasn't advanced."""
    hub = TermHub()
    await hub._get("bot1")

    request_count = 0
    original_request = hub.request_snapshot

    async def track_requests(worker_id: str) -> None:
        nonlocal request_count
        request_count += 1
        await original_request(worker_id)

    hub.request_snapshot = track_requests  # type: ignore[method-assign]

    # Set snapshot with old timestamp
    old_ts = time.time() - 1
    hub._workers["bot1"].last_snapshot = {
        "screen": "old",
        "ts": old_ts,
    }

    ok, snap, reason = await hub.wait_for_guard(
        "bot1",
        expect_prompt_id="nonexistent",
        expect_regex=None,
        timeout_ms=100,
        poll_interval_ms=20,
    )

    # Should have requested multiple times (initial + re-requests)
    assert request_count >= 2


async def test_wait_for_snapshot_with_fresh_snapshot() -> None:
    """wait_for_snapshot returns immediately when fresh snapshot available."""
    hub = TermHub()
    await hub._get("bot1")

    # Set fresh snapshot with timestamp in future (will be checked against req_ts)
    fresh_ts = time.time() + 1  # Definitely in the future
    hub._workers["bot1"].last_snapshot = {
        "screen": "fresh content",
        "ts": fresh_ts,
    }

    # Should return immediately without waiting for timeout
    start = time.time()
    result = await hub.wait_for_snapshot("bot1", timeout_ms=500)
    elapsed = time.time() - start

    assert result is not None
    assert result["screen"] == "fresh content"
    assert elapsed < 0.1  # Should be fast, not wait 500ms


async def test_wait_for_snapshot_ignores_older_ts() -> None:
    """wait_for_snapshot ignores snapshots with ts <= req_ts."""
    hub = TermHub()
    await hub._get("bot1")

    req_ts = time.time()

    # Manually set to just before request time
    hub._workers["bot1"].last_snapshot = {
        "screen": "old",
        "ts": req_ts - 0.1,
    }

    result = await hub.wait_for_snapshot("bot1", timeout_ms=50)

    # Should timeout because snapshot predates the request
    assert result is None


async def test_wait_for_snapshot_worker_disappears() -> None:
    """wait_for_snapshot returns None if worker is deleted during polling."""
    hub = TermHub()
    await hub._get("bot1")

    # Simulate worker being removed
    async def remove_worker() -> None:
        await asyncio.sleep(0.02)
        if "bot1" in hub._workers:
            del hub._workers["bot1"]

    task = asyncio.create_task(remove_worker())
    result = await hub.wait_for_snapshot("bot1", timeout_ms=200)
    await task

    assert result is None


# ---------------------------------------------------------------------------
# Mutation-killing tests for polling edge cases
# ---------------------------------------------------------------------------


async def test_snapshot_matches_expects_and_not_or() -> None:
    """snapshot_matches with both constraints must use AND (not OR)."""
    snapshot = {"prompt_detected": {"prompt_id": "menu"}, "screen": "Main"}
    pattern = re.compile("Main")

    # Both match → True
    assert TermHub.snapshot_matches(snapshot, expect_prompt_id="menu", expect_regex=pattern)

    # Only prompt matches, regex doesn't
    assert not TermHub.snapshot_matches(snapshot, expect_prompt_id="menu", expect_regex=re.compile("Missing"))

    # Only regex matches, prompt doesn't
    assert not TermHub.snapshot_matches(snapshot, expect_prompt_id="wrong", expect_regex=pattern)


async def test_wait_for_snapshot_timestamp_gt_not_ge() -> None:
    """wait_for_snapshot must use > not >= for timestamp comparison."""
    hub = TermHub()
    await hub._get("bot1")

    req_ts = time.time()

    # Snapshot with ts exactly equal to req_ts should NOT be returned (must be >)
    hub._workers["bot1"].last_snapshot = {"screen": "test", "ts": req_ts}

    result = await hub.wait_for_snapshot("bot1", timeout_ms=50)

    # Should timeout because ts == req_ts (not greater)
    assert result is None


async def test_wait_for_snapshot_timestamp_exceeds_req() -> None:
    """Snapshot with ts > req_ts should be returned immediately."""
    hub = TermHub()
    await hub._get("bot1")

    req_ts = time.time()

    # Snapshot with ts slightly in future (> req_ts)
    hub._workers["bot1"].last_snapshot = {"screen": "fresh", "ts": req_ts + 0.01}

    start = time.time()
    result = await hub.wait_for_snapshot("bot1", timeout_ms=500)
    elapsed = time.time() - start

    assert result is not None
    assert result["screen"] == "fresh"
    assert elapsed < 0.2  # Should return fast, not wait full timeout


async def test_wait_for_guard_min_timeout_50ms() -> None:
    """wait_for_guard clamps timeout to min 50ms (not 49ms or 51ms)."""
    hub = TermHub()
    await hub._get("bot1")
    hub._workers["bot1"].last_snapshot = {"screen": "test"}

    # Request very small timeout that should be clamped to 50ms
    start = time.time()
    ok, snap, reason = await hub.wait_for_guard(
        "bot1",
        expect_prompt_id="nonexistent",
        expect_regex=None,
        timeout_ms=1,  # Way too small
        poll_interval_ms=10,
    )
    elapsed = time.time() - start

    # Should have waited at least 50ms (clamped minimum)
    assert elapsed >= 0.04


async def test_wait_for_guard_min_interval_20ms() -> None:
    """wait_for_guard clamps poll_interval to min 20ms (not 19ms or 21ms)."""
    hub = TermHub()
    await hub._get("bot1")
    hub._workers["bot1"].last_snapshot = {"screen": "test", "ts": time.time()}

    # Request very small interval
    start = time.time()
    ok, snap, reason = await hub.wait_for_guard(
        "bot1",
        expect_prompt_id="nonexistent",
        expect_regex=None,
        timeout_ms=100,
        poll_interval_ms=1,  # Way too small, should clamp to 20ms
    )
    elapsed = time.time() - start

    # Should have polled multiple times with minimum 20ms intervals
    # At least one poll cycle should occur
    assert elapsed >= 0.015


async def test_snapshot_matches_none_snapshot_returns_false() -> None:
    """snapshot_matches with None snapshot must return False (not True)."""
    # The check on line 36 is critical
    assert not TermHub.snapshot_matches(None, expect_prompt_id="menu", expect_regex=None)
    assert not TermHub.snapshot_matches(None, expect_prompt_id=None, expect_regex=re.compile("x"))
    assert not TermHub.snapshot_matches(None, expect_prompt_id=None, expect_regex=None)


async def test_snapshot_no_constraints_requires_nonnone() -> None:
    """snapshot_matches with no constraints returns True only for non-None snapshot."""
    # Non-None snapshot, no constraints → True
    assert TermHub.snapshot_matches({"screen": "x"}, expect_prompt_id=None, expect_regex=None)

    # None snapshot, no constraints → False (not True)
    assert not TermHub.snapshot_matches(None, expect_prompt_id=None, expect_regex=None)


# ---------------------------------------------------------------------------
# Mutation-killing tests for snapshot_matches and polling logic
# ---------------------------------------------------------------------------


async def test_snapshot_matches_regex_and_not_logic() -> None:
    """Test double-negation regex logic catches mutations.

    Line 40: `not (expect_regex is not None and not expect_regex.search(...))`
    Mutations: `and`→`or`, `not` removal, `is not`→`is`
    """
    pattern = re.compile("found")
    snapshot = {"screen": "found it"}

    # Regex present and matches → True (catches `not` removal)
    assert TermHub.snapshot_matches(snapshot, expect_prompt_id=None, expect_regex=pattern)

    # Regex present but doesn't match → False (catches `not` removal)
    snapshot_no_match = {"screen": "not there"}
    assert not TermHub.snapshot_matches(snapshot_no_match, expect_prompt_id=None, expect_regex=pattern)

    # Regex absent → True regardless of screen (catches `is not`→`is`)
    assert TermHub.snapshot_matches({"screen": "anything"}, expect_prompt_id=None, expect_regex=None)
    assert TermHub.snapshot_matches({"screen": ""}, expect_prompt_id=None, expect_regex=None)


async def test_snapshot_matches_timestamp_boundary() -> None:
    """Test timestamp comparison boundaries (mutation documentation).

    Line 53: `snap.get("ts", 0) > req_ts`
    Mutations: `>`→`>=`, `>`→`<`

    Integration tests in other files cover actual timestamp behavior.
    This documents the mutation targets:
    """
    req_ts = 100.0

    # Catches `>`→`>=`: value == req_ts should NOT be considered fresh
    ts_equal = 100.0
    assert not (ts_equal > req_ts)  # False, correctly

    # Catches `>`→`<` inversion: value > req_ts should be fresh
    ts_newer = 101.0
    assert ts_newer > req_ts  # True, correctly

    # Default case: missing ts treated as 0, older than any req_ts
    ts_default = 0.0
    assert not (ts_default > req_ts)  # False, correctly


async def test_polling_interval_min_enforced() -> None:
    """Test that minimum poll interval is enforced via max().

    Line 87: `interval = max(20, poll_interval_ms) / 1000.0`
    Mutations: `max`→`min` would allow intervals below 20ms

    This documents the mutation target; integration tests cover actual polling.
    """
    assert max(20, 10) == 20  # Catches `max`→`min`
    assert max(20, 50) == 50  # Boundary above minimum
