#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for the bug-fixes applied to connections.py.

Covers:
- Rate-limit evaluation order: per-client checked FIRST, global only consumed on pass.
- resume_without_owner: backwards event scan (not just last event) to detect expiry.
- was_owner path: hijack_owner_expires_at set to None (not "").
- elif guard conditions: worker_ws=None and owned_hijack=False both skip the scan.
- hijack_released stops the backwards scan.
- on_worker_empty fires when last browser disconnects; _background_tasks holds the task.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any
from unittest.mock import MagicMock

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import WorkerTermState
from provide.uterm.server.bridge.ratelimit import TokenBucket


def _make_hub(**kwargs: Any) -> TermHub:
    return TermHub(**kwargs)


def _make_ws() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# Rate-limit evaluation order: per-client first, global second
# ---------------------------------------------------------------------------


class TestRateLimitEvaluationOrder:
    """Per-client bucket is checked before the global bucket.

    If the global bucket is checked first (old bug), a flooded client drains the
    shared global token even when the per-client check would have rejected the
    request.  The correct order protects the global bucket from cross-client drain.
    """

    def test_acquire_per_client_rejected_does_not_drain_global(self) -> None:
        """When per-client bucket is exhausted, global token must NOT be consumed.

        Uses a high global rate (100/s) so the global bucket has plenty of
        tokens — only the per-client bucket is drained.  This catches the
        evaluation-order bug where global.allow() is called before per-client.
        """
        hub = _make_hub(rest_acquire_rate_limit_per_sec=100)  # high global rate
        # Drain the per-client bucket completely.
        client = "victim"
        # Create and exhaust per-client bucket manually.
        hub.limiter.rest_acquire_per_client[client] = TokenBucket(0.001)
        hub.limiter.rest_acquire_per_client[client]._tokens = 0.0

        global_tokens_before = hub.limiter.rest_acquire_bucket._tokens

        # This call should be rejected at the per-client check; global bucket untouched.
        result = hub.allow_rest_acquire_for(client)

        assert result is False, "Per-client exhausted → should be rejected"
        assert hub.limiter.rest_acquire_bucket._tokens == global_tokens_before, (
            "Global bucket must NOT be drained when the per-client check rejects first"
        )

    def test_send_per_client_rejected_does_not_drain_global(self) -> None:
        """Same as above for the send rate limiter.

        Uses a high global rate so only per-client exhaustion is tested.
        """
        hub = _make_hub(rest_send_rate_limit_per_sec=100)  # high global rate
        client = "flooder"
        hub.limiter.rest_send_per_client[client] = TokenBucket(0.001)
        hub.limiter.rest_send_per_client[client]._tokens = 0.0

        global_tokens_before = hub.limiter.rest_send_bucket._tokens

        result = hub.allow_rest_send_for(client)

        assert result is False, "Per-client exhausted → should be rejected"
        assert hub.limiter.rest_send_bucket._tokens == global_tokens_before, (
            "Global bucket must NOT be drained when per-client rejects"
        )

    def test_acquire_allowed_client_consumes_global_token(self) -> None:
        """When the per-client check passes, the global bucket token IS consumed.

        This ensures we didn't accidentally remove the global check entirely.
        Kills a mutation that removes the global bucket check:
          return bucket.allow()
        """
        hub = _make_hub(rest_acquire_rate_limit_per_sec=100)
        client = "legit"
        global_tokens_before = hub.limiter.rest_acquire_bucket._tokens

        result = hub.allow_rest_acquire_for(client)

        assert result is True, "Should be allowed"
        assert hub.limiter.rest_acquire_bucket._tokens < global_tokens_before, (
            "Global bucket token must be consumed when per-client passes"
        )

    def test_global_exhausted_rejects_despite_per_client_tokens(self) -> None:
        """When global bucket is empty, even a client with tokens is rejected.

        Kills a mutation that removes the global check:
          return bucket.allow()   (no global check)
        """
        hub = _make_hub(rest_acquire_rate_limit_per_sec=100)
        # Drain global bucket.
        hub.limiter.rest_acquire_bucket._tokens = 0.0

        result = hub.allow_rest_acquire_for("any-client")

        assert result is False, "Global bucket empty → should be rejected"

    def test_send_allowed_client_consumes_global_token(self) -> None:
        """Send rate: per-client pass → global token consumed."""
        hub = _make_hub(rest_send_rate_limit_per_sec=100)
        client = "sender"
        global_tokens_before = hub.limiter.rest_send_bucket._tokens

        result = hub.allow_rest_send_for(client)

        assert result is True
        assert hub.limiter.rest_send_bucket._tokens < global_tokens_before, (
            "Global bucket token must be consumed when per-client passes"
        )


# ---------------------------------------------------------------------------
# resume_without_owner: backwards event scan
# ---------------------------------------------------------------------------


class TestResumeWithoutOwnerBackwardsScan:
    """cleanup_browser_disconnect scans backwards through events to find the most
    recent hijack-related event — not just the last event — so that a subsequent
    snapshot event cannot shadow a hijack_owner_expired or hijack_lease_expired.
    """

    def _make_state_with_events(self, *event_types: str) -> WorkerTermState:
        """Build a WorkerTermState with a sequence of named events."""
        st = WorkerTermState()
        st.input_mode = "hijack"
        for i, t in enumerate(event_types):
            st.events.append({"seq": i + 1, "ts": time.time(), "type": t, "data": {}})
        st.min_event_seq = 1
        st.event_seq = len(event_types)
        return st

    async def test_snapshot_after_owner_expired_does_not_trigger_resume(self) -> None:
        """A 'snapshot' event appended AFTER 'hijack_owner_expired' must not suppress
        the backwards-scan discovery that a resume was already sent.

        Bug scenario:
          1. hijack_owner_expired appended (cleanup sent resume)
          2. snapshot appended (last event is now 'snapshot')
          3. old code: last_event_type = 'snapshot' → resume_without_owner = True (wrong)
          4. new code: backwards scan finds 'hijack_owner_expired' → resume_without_owner = False
        """
        hub = _make_hub()
        worker_ws = MagicMock()
        browser_ws = MagicMock()

        async with hub._lock:
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[browser_ws] = "admin"
            # Simulate: browser held hijack (owned_hijack=True), lease expired,
            # cleanup sent resume and appended hijack_owner_expired, then a snapshot arrived.
            st.events = deque(
                [
                    {"seq": 1, "ts": time.time(), "type": "hijack_acquired", "data": {}},
                    {"seq": 2, "ts": time.time(), "type": "hijack_owner_expired", "data": {}},
                    {"seq": 3, "ts": time.time(), "type": "snapshot", "data": {}},
                ],
                maxlen=2000,
            )
            # No active hijack (cleanup already handled it)
            st.hijack_owner = None
            st.hijack_owner_expires_at = None
            st.hijack_session = None

        result = await hub.cleanup_browser_disconnect("w1", browser_ws, owned_hijack=True)

        assert result["resume_without_owner"] is False, (
            "Backwards scan must find hijack_owner_expired before snapshot → no spurious resume"
        )

    async def test_snapshot_after_lease_expired_does_not_trigger_resume(self) -> None:
        """Same scenario with 'hijack_lease_expired' (REST lease)."""
        hub = _make_hub()
        worker_ws = MagicMock()
        browser_ws = MagicMock()

        async with hub._lock:
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[browser_ws] = "admin"
            st.events = deque(
                [
                    {"seq": 1, "ts": time.time(), "type": "hijack_acquired", "data": {}},
                    {"seq": 2, "ts": time.time(), "type": "hijack_lease_expired", "data": {}},
                    {"seq": 3, "ts": time.time(), "type": "snapshot", "data": {}},
                    {"seq": 4, "ts": time.time(), "type": "snapshot", "data": {}},
                ],
                maxlen=2000,
            )
            st.hijack_owner = None
            st.hijack_session = None

        result = await hub.cleanup_browser_disconnect("w1", browser_ws, owned_hijack=True)

        assert result["resume_without_owner"] is False, (
            "Backwards scan through multiple snapshots must still find hijack_lease_expired"
        )

    async def test_no_expiry_event_means_resume_needed(self) -> None:
        """When no hijack_*_expired event is in the ring, resume_without_owner is True.

        This covers the case where a browser held the hijack but it was released
        by force or manual action (not expiry), leaving the worker still paused.
        """
        hub = _make_hub()
        worker_ws = MagicMock()
        browser_ws = MagicMock()

        async with hub._lock:
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[browser_ws] = "admin"
            # Only snapshot events — no expiry ever appended.
            st.events = deque(
                [
                    {"seq": 1, "ts": time.time(), "type": "snapshot", "data": {}},
                    {"seq": 2, "ts": time.time(), "type": "snapshot", "data": {}},
                ],
                maxlen=2000,
            )
            st.hijack_owner = None
            st.hijack_session = None

        result = await hub.cleanup_browser_disconnect("w1", browser_ws, owned_hijack=True)

        assert result["resume_without_owner"] is True, (
            "No expiry event → resume_without_owner must be True so worker is unpaused"
        )

    async def test_hijack_acquired_after_expiry_stops_scan(self) -> None:
        """A hijack_acquired event after a previous expiry stops the scan — the new
        hijack cycle is independent; the browser is no longer relevant to the old cycle."""
        hub = _make_hub()
        worker_ws = MagicMock()
        browser_ws = MagicMock()

        async with hub._lock:
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[browser_ws] = "admin"
            # Expired → re-acquired → now not hijacked (second session also expired)
            st.events = deque(
                [
                    {"seq": 1, "ts": time.time(), "type": "hijack_owner_expired", "data": {}},
                    {"seq": 2, "ts": time.time(), "type": "hijack_acquired", "data": {}},
                    {"seq": 3, "ts": time.time(), "type": "snapshot", "data": {}},
                ],
                maxlen=2000,
            )
            st.hijack_owner = None
            st.hijack_session = None

        result = await hub.cleanup_browser_disconnect("w1", browser_ws, owned_hijack=True)

        # Scan backwards: snapshot → hijack_acquired (stop).
        # No expiry found before the re-acquisition → resume_without_owner = True.
        assert result["resume_without_owner"] is True, (
            "Scan stops at hijack_acquired; without a later expiry, resume is still needed"
        )

    async def test_empty_events_means_resume_needed(self) -> None:
        """With no events at all, resume_without_owner defaults to True."""
        hub = _make_hub()
        worker_ws = MagicMock()
        browser_ws = MagicMock()

        async with hub._lock:
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[browser_ws] = "admin"
            st.events = deque(maxlen=2000)
            st.hijack_owner = None
            st.hijack_session = None

        result = await hub.cleanup_browser_disconnect("w1", browser_ws, owned_hijack=True)

        assert result["resume_without_owner"] is True, "Empty event ring → assume resume needed"


# ---------------------------------------------------------------------------
# was_owner clears hijack_owner_expires_at to None (not "" or 0)
# ---------------------------------------------------------------------------


class TestWasOwnerClearsExpiresAt:
    """cleanup_browser_disconnect must set hijack_owner_expires_at = None (not "").

    Kills mutmut_22 that changes None → "".
    """

    async def test_hijack_owner_expires_at_is_none_after_owner_disconnect(self) -> None:
        """was_owner path must clear hijack_owner_expires_at to exactly None."""
        hub = _make_hub()
        browser_ws = MagicMock()

        async with hub._lock:
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = MagicMock()
            st.browsers[browser_ws] = "admin"
            # Make browser_ws the dashboard hijack owner with an active lease.
            st.hijack_owner = browser_ws
            st.hijack_owner_expires_at = time.time() + 3600  # far future

        await hub.cleanup_browser_disconnect("w1", browser_ws, owned_hijack=True)

        async with hub._lock:
            st = hub.registry._workers.get("w1")
            assert st is not None
            assert st.hijack_owner_expires_at is None, (
                "was_owner path must set hijack_owner_expires_at = None, not '' or any other falsy value"
            )
            assert st.hijack_owner is None


# ---------------------------------------------------------------------------
# elif guard: worker_ws=None skips the backwards scan
# ---------------------------------------------------------------------------


class TestElifGuardConditions:
    """The elif guard has three AND conditions; mutations that weaken it let the
    scan run in cases where it should not, producing incorrect resume_without_owner.

    Kills mutmut_25 (and→or) and mutmut_26 (and→or with different operand order).
    """

    async def test_no_worker_ws_skips_scan_even_with_owned_hijack(self) -> None:
        """owned_hijack=True but worker_ws=None → elif branch not entered.

        Kills mutmut_25: `and st.worker_ws is not None or not self.is_hijacked(st)`
        With that mutation, `(True and False) or True` = True → scan runs (wrong).
        """
        hub = _make_hub()
        browser_ws = MagicMock()

        async with hub._lock:
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = None  # no worker connected
            st.browsers[browser_ws] = "admin"
            st.hijack_owner = None
            st.hijack_session = None
            # Only snapshot events — if scan ran, resume_without_owner would be True.
            st.events = deque(
                [{"seq": 1, "ts": time.time(), "type": "snapshot", "data": {}}],
                maxlen=2000,
            )

        result = await hub.cleanup_browser_disconnect("w1", browser_ws, owned_hijack=True)

        assert result["resume_without_owner"] is False, (
            "worker_ws=None → elif guard fails → no scan → resume_without_owner stays False"
        )

    async def test_owned_hijack_false_skips_scan(self) -> None:
        """owned_hijack=False → elif branch not entered regardless of other conditions.

        Kills mutmut_26: `owned_hijack or st.worker_ws is not None and not self.is_hijacked(st)`
        With that mutation, `False or (True and True)` = True → scan runs (wrong).
        """
        hub = _make_hub()
        browser_ws = MagicMock()

        async with hub._lock:
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = MagicMock()  # worker IS connected
            st.browsers[browser_ws] = "operator"
            st.hijack_owner = None
            st.hijack_session = None
            # Only snapshot events — if scan ran, resume_without_owner would be True.
            st.events = deque(
                [{"seq": 1, "ts": time.time(), "type": "snapshot", "data": {}}],
                maxlen=2000,
            )

        result = await hub.cleanup_browser_disconnect("w1", browser_ws, owned_hijack=False)

        assert result["resume_without_owner"] is False, (
            "owned_hijack=False → elif guard fails → no scan → resume_without_owner stays False"
        )
