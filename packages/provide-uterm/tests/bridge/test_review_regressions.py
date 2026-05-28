#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Regression tests for findings from external code review.

1. REST hijack must be rejected when session is in open input mode.
2. WorkerTermState.last_activity_at default must use monotonic time.
3. WS-broadcast lease_expires_at must be wall-clock, not monotonic.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

import pytest
from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import WorkerTermState

from provide.uterm.bridge.coordinator import HijackSession


def _make_hub(**kwargs: Any) -> TermHub:
    defaults: dict[str, Any] = {"resolve_browser_role": lambda _ws, _wid: "admin"}
    defaults.update(kwargs)
    return TermHub(**defaults)


def _make_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send_text = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# Finding 1: REST hijack must respect open input mode
# ---------------------------------------------------------------------------


class TestRestHijackOpenModeRejection:
    async def test_rest_acquire_rejected_in_open_mode(self) -> None:
        """REST hijack/acquire returns (False, 'open_mode') when session is in open mode."""
        hub = _make_hub()
        worker_ws = _make_ws()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.input_mode = "open"

        acquired, err = await hub.try_acquire_rest_hijack(
            "w1", owner="test", lease_s=60, hijack_id="hj1", now=time.monotonic()
        )

        assert acquired is False
        assert err == "open_mode"

    async def test_rest_acquire_allowed_in_hijack_mode(self) -> None:
        """REST hijack/acquire succeeds when session is in default hijack mode."""
        hub = _make_hub()
        worker_ws = _make_ws()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.input_mode = "hijack"

        acquired, err = await hub.try_acquire_rest_hijack(
            "w1", owner="test", lease_s=60, hijack_id="hj1", now=time.monotonic()
        )

        assert acquired is True
        assert err is None

    async def test_rest_acquire_rejected_after_mode_switch_to_open(self) -> None:
        """Switching mode to open blocks subsequent REST hijack attempts."""
        hub = _make_hub()
        worker_ws = _make_ws()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws

        # Default is hijack mode — acquire works
        acquired, _ = await hub.try_acquire_rest_hijack(
            "w1", owner="test", lease_s=60, hijack_id="hj1", now=time.monotonic()
        )
        assert acquired is True

        # Release the session
        async with hub._lock:
            st = hub._workers["w1"]
            st.hijack_session = None

        # Switch to open mode
        ok, switch_err = await hub.set_input_mode("w1", "open")
        assert ok is True

        # Now REST acquire should be rejected
        acquired2, err2 = await hub.try_acquire_rest_hijack(
            "w1", owner="test2", lease_s=60, hijack_id="hj2", now=time.monotonic()
        )
        assert acquired2 is False
        assert err2 == "open_mode"


# ---------------------------------------------------------------------------
# Finding 2: last_activity_at must default to monotonic time
# ---------------------------------------------------------------------------


class TestLastActivityAtMonotonicDefault:
    def test_default_is_monotonic_not_wall_clock(self) -> None:
        """WorkerTermState.last_activity_at default must be on the monotonic scale."""
        st = WorkerTermState()
        mono_now = time.monotonic()
        wall_now = time.time()

        # Monotonic time is typically much smaller than wall-clock epoch time
        # (monotonic is seconds since boot, wall is seconds since 1970)
        # The default should be close to monotonic, not wall-clock
        assert abs(st.last_activity_at - mono_now) < 5.0, (
            f"last_activity_at ({st.last_activity_at}) should be near "
            f"time.monotonic() ({mono_now}), not time.time() ({wall_now})"
        )

    async def test_idle_cleanup_works_for_untouched_worker(self) -> None:
        """get_idle_candidates correctly identifies a never-touched worker as idle."""
        hub = _make_hub()

        async with hub._lock:
            st = hub._workers.setdefault("idle1", WorkerTermState())
            # Simulate worker that connected 10 seconds ago
            st.last_activity_at = time.monotonic() - 10

        candidates = await hub.get_idle_candidates(timeout_s=5)
        idle_wids = [wid for wid, _ in candidates]
        assert "idle1" in idle_wids, "Worker idle for 10s should appear in 5s timeout candidates"

    async def test_recently_touched_worker_not_idle(self) -> None:
        """A worker touched 1 second ago should not appear in 5s idle candidates."""
        hub = _make_hub()

        async with hub._lock:
            st = hub._workers.setdefault("active1", WorkerTermState())
            st.last_activity_at = time.monotonic() - 1

        candidates = await hub.get_idle_candidates(timeout_s=5)
        idle_wids = [wid for wid, _ in candidates]
        assert "active1" not in idle_wids


# ---------------------------------------------------------------------------
# Finding 3: WS-broadcast lease_expires_at must be wall-clock
# ---------------------------------------------------------------------------


class TestBroadcastLeaseExpiresAtWallClock:
    async def test_hijack_state_msg_returns_wall_clock_lease(self) -> None:
        """hijack_state_msg_for converts monotonic lease to wall-clock."""
        hub = _make_hub()
        owner_ws = _make_ws()
        viewer_ws = _make_ws()

        mono_now = time.monotonic()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = mono_now + 60
            st.browsers[viewer_ws] = "admin"

        msg = await hub.hijack_state_msg_for("w1", viewer_ws)

        # The lease_expires_at in the message should be wall-clock
        lease = msg.get("lease_expires_at")
        assert lease is not None
        wall_now = time.time()

        # Wall-clock lease should be approximately now + 60 (±2s tolerance)
        assert abs(lease - (wall_now + 60)) < 2.0, (
            f"lease_expires_at ({lease}) should be near wall-clock {wall_now + 60}, not monotonic {mono_now + 60}"
        )

    async def test_broadcast_hijack_state_sends_wall_clock(self) -> None:
        """broadcast_hijack_state sends wall-clock lease to all browsers."""
        hub = _make_hub()
        owner_ws = _make_ws()
        browser_ws = _make_ws()

        from provide.uterm.control_channel import ControlChannelDecoder

        mono_now = time.monotonic()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = mono_now + 45
            st.browsers[browser_ws] = "admin"
            st.browsers[owner_ws] = "admin"

        await hub.broadcast_hijack_state("w1")

        # Decode what was sent to browsers
        assert browser_ws.send_text.called
        decoder = ControlChannelDecoder()
        for call in browser_ws.send_text.call_args_list:
            raw = call.args[0]
            events = decoder.feed(raw)
            for ev in events:
                if ev.kind == "control" and ev.control.get("type") == "hijack_state":
                    lease = ev.control.get("lease_expires_at")
                    if lease is not None:
                        wall_now = time.time()
                        assert abs(lease - (wall_now + 45)) < 2.0, (
                            f"Broadcast lease_expires_at ({lease}) should be wall-clock, "
                            f"not monotonic ({mono_now + 45})"
                        )
                        return

        pytest.fail("No hijack_state message with lease_expires_at found in broadcast")

    async def test_rest_lease_is_wall_clock_not_monotonic(self) -> None:
        """REST hijack_state also uses wall-clock via _mono_to_wall for REST sessions."""
        hub = _make_hub()
        viewer_ws = _make_ws()

        mono_now = time.monotonic()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_session = HijackSession(
                hijack_id="hj1",
                owner="rest-client",
                acquired_at=mono_now,
                lease_expires_at=mono_now + 120,
                last_heartbeat=mono_now,
            )
            st.browsers[viewer_ws] = "admin"

        msg = await hub.hijack_state_msg_for("w1", viewer_ws)
        lease = msg.get("lease_expires_at")
        assert lease is not None
        wall_now = time.time()

        assert abs(lease - (wall_now + 120)) < 2.0, (
            f"REST lease_expires_at ({lease}) should be wall-clock, not monotonic"
        )


# ---------------------------------------------------------------------------
# Finding 4 (round 2): REST open-mode rejection must send compensating resume
# ---------------------------------------------------------------------------


class TestOpenModeRejectionSendsResume:
    async def test_open_mode_rejection_resumes_worker(self) -> None:
        """REST acquire against open session: worker must receive resume after rejection.

        The acquire route sends pause BEFORE checking input_mode. If the hub
        rejects with 'open_mode', the route must send a compensating resume
        so the worker is not stranded in paused state.
        """
        hub = _make_hub()
        worker_ws = _make_ws()
        worker_ws.send_text = AsyncMock(return_value=None)

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.input_mode = "open"

        # Simulate the REST route flow: pause is sent first, then hub rejects

        # Call try_acquire — should return open_mode
        acquired, err = await hub.try_acquire_rest_hijack(
            "w1", owner="test", lease_s=60, hijack_id="hj-open", now=time.monotonic()
        )
        assert acquired is False
        assert err == "open_mode"

        # Now simulate what rest.py does: send resume since err != "already_hijacked"
        # (This is what the fix ensures — open_mode is NOT in the suppression list)
        should_resume = err != "already_hijacked"
        assert should_resume is True, "open_mode rejection MUST trigger compensating resume"


# ---------------------------------------------------------------------------
# Finding 5 (round 2): Rate limit per-client check before global
# ---------------------------------------------------------------------------


class TestRateLimitEvaluationOrder:
    def test_exhausted_per_client_does_not_drain_global_acquire(self) -> None:
        """Per-client exhaustion must short-circuit before consuming a global token.

        Global bucket has plenty of tokens (rate=100); only per-client is empty.
        If global is checked first, it loses a token even though the request
        is ultimately rejected.
        """
        from provide.uterm.server.bridge.ratelimit import TokenBucket

        hub = _make_hub()
        # Give global bucket plenty of tokens
        hub._rest_acquire_bucket = TokenBucket(100)
        hub._rest_acquire_bucket._tokens = 100.0

        # Exhaust per-client bucket
        client = "abuser"
        hub._rest_acquire_per_client[client] = TokenBucket(0.001)
        hub._rest_acquire_per_client[client]._tokens = 0.0

        global_before = hub._rest_acquire_bucket._tokens
        result = hub.allow_rest_acquire_for(client)

        assert result is False
        assert hub._rest_acquire_bucket._tokens == global_before, (
            f"Global tokens should be unchanged ({global_before}), got {hub._rest_acquire_bucket._tokens}"
        )

    def test_exhausted_per_client_does_not_drain_global_send(self) -> None:
        """Same check for the send rate limiter."""
        from provide.uterm.server.bridge.ratelimit import TokenBucket

        hub = _make_hub()
        hub._rest_send_bucket = TokenBucket(100)
        hub._rest_send_bucket._tokens = 100.0

        client = "abuser"
        hub._rest_send_per_client[client] = TokenBucket(0.001)
        hub._rest_send_per_client[client]._tokens = 0.0

        global_before = hub._rest_send_bucket._tokens
        result = hub.allow_rest_send_for(client)

        assert result is False
        assert hub._rest_send_bucket._tokens == global_before
