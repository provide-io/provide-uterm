#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Coverage tests for hijack/hub/connections.py — eviction and set_worker_hello_mode branches."""

from __future__ import annotations

from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.hub import connections as _conn_module

# ---------------------------------------------------------------------------
# allow_rest_send_for — LRU eviction (lines 93-94)
# ---------------------------------------------------------------------------


def test_allow_rest_send_for_evicts_on_overflow() -> None:
    """Lines 93-94: when _rest_send_per_client reaches max, oldest half are evicted."""
    hub = TermHub()
    cap = _conn_module._REST_CLIENT_CACHE_MAX

    # Fill dict just to the cap by bypassing the public method (avoids rate limit logic).
    from provide.terminal.hijack.ratelimit import TokenBucket

    hub._rest_send_per_client = {f"c{i}": TokenBucket(1) for i in range(cap)}

    # One more call should trigger eviction path
    result = hub.allow_rest_send_for("new-client")
    assert isinstance(result, bool)
    # Dict shrunk by eviction then grew by 1 → total = cap - (cap//2) + 1
    expected = cap - cap // 2 + 1
    assert len(hub._rest_send_per_client) == expected


# ---------------------------------------------------------------------------
# set_worker_hello_mode — blocked by active hijack (lines 139-146)
# ---------------------------------------------------------------------------


async def test_set_worker_hello_mode_blocked_when_hijack_active() -> None:
    """Lines 141-146: switching to 'open' while hijack is active → returns False, mode unchanged."""
    hub = TermHub()
    # Register a worker (creates WorkerTermState)
    worker_id = "w-hello-block"
    from unittest.mock import AsyncMock, MagicMock

    ws = MagicMock()
    ws.send_text = AsyncMock()

    async with hub._lock:
        from provide.terminal.hijack.models import WorkerTermState

        st = WorkerTermState()
        hub._workers[worker_id] = st

    # Activate a hijack lease
    import time

    from provide.terminal.hijack.models import HijackSession

    now = time.time()
    async with hub._lock:
        hub._workers[worker_id].hijack_session = HijackSession(
            hijack_id="test-hid",
            owner="alice",
            acquired_at=now,
            lease_expires_at=now + 60,
            last_heartbeat=now,
        )

    # Attempting to switch to open while hijack is active should be blocked
    result = await hub.set_worker_hello_mode(worker_id, "open")
    assert result is False
    assert hub._workers[worker_id].input_mode == "hijack"


async def test_set_worker_hello_mode_returns_false_unknown_worker() -> None:
    """Line 139: unknown worker_id → returns False immediately."""
    hub = TermHub()
    result = await hub.set_worker_hello_mode("does-not-exist", "open")
    assert result is False


async def test_set_worker_hello_mode_succeeds_when_no_hijack() -> None:
    """Line 147: no active hijack → mode is updated, returns True."""
    hub = TermHub()
    worker_id = "w-hello-ok"
    async with hub._lock:
        from provide.terminal.hijack.models import WorkerTermState

        hub._workers[worker_id] = WorkerTermState()

    result = await hub.set_worker_hello_mode(worker_id, "open")
    assert result is True
    assert hub._workers[worker_id].input_mode == "open"


# ---------------------------------------------------------------------------
# force_release_hijack — with REST-style hijack_session (lines 265-267)
# ---------------------------------------------------------------------------


async def test_force_release_hijack_clears_rest_session() -> None:
    """Lines 265-267: force_release clears hijack_session and reports owner."""
    hub = TermHub()
    worker_id = "w-force-rest"
    async with hub._lock:
        import time

        from provide.terminal.hijack.models import HijackSession, WorkerTermState

        now = time.time()
        st = WorkerTermState()
        st.hijack_session = HijackSession(
            hijack_id="force-hid",
            owner="bob",
            acquired_at=now,
            lease_expires_at=now + 60,
            last_heartbeat=now,
        )
        hub._workers[worker_id] = st

    result = await hub.force_release_hijack(worker_id)
    assert result is True
    assert hub._workers[worker_id].hijack_session is None
