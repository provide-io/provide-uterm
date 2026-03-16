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


# ---------------------------------------------------------------------------
# allow_rest_acquire_for — eviction and both-bucket-check
# ---------------------------------------------------------------------------


def test_allow_rest_acquire_for_evicts_on_overflow() -> None:
    """LRU eviction for acquire bucket."""
    hub = TermHub()
    cap = _conn_module._REST_CLIENT_CACHE_MAX
    from provide.terminal.hijack.ratelimit import TokenBucket

    hub._rest_acquire_per_client = {f"c{i}": TokenBucket(1) for i in range(cap)}

    result = hub.allow_rest_acquire_for("new-client")
    assert isinstance(result, bool)
    expected = cap - cap // 2 + 1
    assert len(hub._rest_acquire_per_client) == expected


def test_allow_rest_acquire_for_checks_both_buckets() -> None:
    """Global bucket allow() must pass for positive result."""
    hub = TermHub()
    from unittest.mock import MagicMock

    hub._rest_acquire_bucket = MagicMock()
    hub._rest_acquire_bucket.allow.return_value = False
    result = hub.allow_rest_acquire_for("client1")
    assert result is False


def test_allow_rest_send_for_checks_both_buckets() -> None:
    """Global send bucket allow() must pass for positive result."""
    hub = TermHub()
    from unittest.mock import MagicMock

    hub._rest_send_bucket = MagicMock()
    hub._rest_send_bucket.allow.return_value = False
    result = hub.allow_rest_send_for("client1")
    assert result is False


# ---------------------------------------------------------------------------
# register_worker — clearing hijack state branches
# ---------------------------------------------------------------------------


async def test_register_worker_clears_all_hijack_fields() -> None:
    """When prev_was_hijacked, all three hijack fields are cleared."""
    hub = TermHub()
    worker_id = "w-clear"
    import time
    from unittest.mock import MagicMock

    from provide.terminal.hijack.models import HijackSession, WorkerTermState

    # Pre-populate with hijack state
    async with hub._lock:
        now = time.time()
        st = WorkerTermState()
        st.hijack_session = HijackSession(
            hijack_id="test",
            owner="alice",
            acquired_at=now,
            lease_expires_at=now + 60,
            last_heartbeat=now,
        )
        st.hijack_owner = MagicMock()
        st.hijack_owner_expires_at = now + 10
        hub._workers[worker_id] = st

    # Register should clear it
    ws = MagicMock()
    result = await hub.register_worker(worker_id, ws)
    assert result is True
    async with hub._lock:
        st = hub._workers[worker_id]
        assert st.hijack_session is None
        assert st.hijack_owner is None
        assert st.hijack_owner_expires_at is None


async def test_register_worker_returns_false_when_no_prior_hijack() -> None:
    """When no hijack was active, returns False."""
    hub = TermHub()
    worker_id = "w-new"
    from unittest.mock import MagicMock

    ws = MagicMock()
    result = await hub.register_worker(worker_id, ws)
    assert result is False


# ---------------------------------------------------------------------------
# is_active_worker — worker mismatch condition
# ---------------------------------------------------------------------------


async def test_is_active_worker_returns_false_on_ws_mismatch() -> None:
    """Return False if st.worker_ws is not the same WebSocket instance."""
    hub = TermHub()
    worker_id = "w-mismatch"
    from unittest.mock import MagicMock

    ws1 = MagicMock()
    ws2 = MagicMock()

    async with hub._lock:
        from provide.terminal.hijack.models import WorkerTermState

        st = WorkerTermState()
        st.worker_ws = ws1
        hub._workers[worker_id] = st

    result = await hub.is_active_worker(worker_id, ws2)
    assert result is False


async def test_is_active_worker_returns_true_on_ws_match() -> None:
    """Return True if st.worker_ws matches the provided WebSocket."""
    hub = TermHub()
    worker_id = "w-match"
    from unittest.mock import MagicMock

    ws = MagicMock()
    async with hub._lock:
        from provide.terminal.hijack.models import WorkerTermState

        st = WorkerTermState()
        st.worker_ws = ws
        hub._workers[worker_id] = st

    result = await hub.is_active_worker(worker_id, ws)
    assert result is True


# ---------------------------------------------------------------------------
# can_send_input — open mode role check
# ---------------------------------------------------------------------------


async def test_can_send_input_open_mode_viewer_denied() -> None:
    """In open mode, viewer role cannot send input."""
    hub = TermHub()
    from unittest.mock import MagicMock

    from provide.terminal.hijack.models import WorkerTermState

    st = WorkerTermState()
    st.input_mode = "open"
    ws = MagicMock()
    st.browsers[ws] = "viewer"

    result = hub.can_send_input(st, ws)
    assert result is False


async def test_can_send_input_open_mode_operator_allowed() -> None:
    """In open mode, operator role can send input."""
    hub = TermHub()
    from unittest.mock import MagicMock

    from provide.terminal.hijack.models import WorkerTermState

    st = WorkerTermState()
    st.input_mode = "open"
    ws = MagicMock()
    st.browsers[ws] = "operator"

    result = hub.can_send_input(st, ws)
    assert result is True


async def test_can_send_input_open_mode_admin_allowed() -> None:
    """In open mode, admin role can send input."""
    hub = TermHub()
    from unittest.mock import MagicMock

    from provide.terminal.hijack.models import WorkerTermState

    st = WorkerTermState()
    st.input_mode = "open"
    ws = MagicMock()
    st.browsers[ws] = "admin"

    result = hub.can_send_input(st, ws)
    assert result is True


async def test_can_send_input_open_mode_missing_role_defaults_viewer() -> None:
    """In open mode, missing role defaults to viewer (no send)."""
    hub = TermHub()
    from unittest.mock import MagicMock

    from provide.terminal.hijack.models import WorkerTermState

    st = WorkerTermState()
    st.input_mode = "open"
    ws = MagicMock()
    # Don't add ws to browsers dict

    result = hub.can_send_input(st, ws)
    assert result is False


# ---------------------------------------------------------------------------
# cleanup_browser_disconnect — complex conditional branches
# ---------------------------------------------------------------------------


async def test_cleanup_browser_disconnect_unknown_worker() -> None:
    """If worker doesn't exist, returns all False."""
    hub = TermHub()
    from unittest.mock import MagicMock

    result = await hub.cleanup_browser_disconnect("nonexistent", MagicMock(), False)
    assert result["was_owner"] is False
    assert result["resume_without_owner"] is False
    assert result["rest_still_active"] is False


async def test_cleanup_browser_disconnect_was_owner_with_rest_active() -> None:
    """If browser was the hijack owner and REST lease is valid, rest_still_active=True."""
    hub = TermHub()
    import time
    from unittest.mock import MagicMock

    from provide.terminal.hijack.models import HijackSession, WorkerTermState

    worker_id = "w-owner"
    ws_owner = MagicMock()
    ws_other = MagicMock()

    async with hub._lock:
        st = WorkerTermState()
        st.worker_ws = MagicMock()
        st.browsers[ws_owner] = "admin"
        st.browsers[ws_other] = "viewer"
        now = time.time()
        st.hijack_session = HijackSession(
            hijack_id="test",
            owner="alice",
            acquired_at=now,
            lease_expires_at=now + 60,
            last_heartbeat=now,
        )
        st.hijack_owner = ws_owner
        st.hijack_owner_expires_at = now + 60
        hub._workers[worker_id] = st

    result = await hub.cleanup_browser_disconnect(worker_id, ws_owner, False)
    assert result["was_owner"] is True
    assert result["rest_still_active"] is True


async def test_cleanup_browser_disconnect_not_owner_triggers_on_worker_empty() -> None:
    """When last browser disconnects, on_worker_empty callback is invoked."""
    hub = TermHub()
    from unittest.mock import MagicMock

    from provide.terminal.hijack.models import WorkerTermState

    worker_id = "w-empty"
    ws = MagicMock()

    async with hub._lock:
        st = WorkerTermState()
        st.browsers[ws] = "viewer"
        hub._workers[worker_id] = st

    # Set callback
    callback_invoked = []

    async def on_empty(wid: str) -> None:
        callback_invoked.append(wid)

    hub.on_worker_empty = on_empty

    import asyncio

    await hub.cleanup_browser_disconnect(worker_id, ws, False)
    await asyncio.sleep(0.05)  # Let background task fire
    assert callback_invoked == [worker_id]


async def test_cleanup_browser_disconnect_resume_without_owner() -> None:
    """When browser owned hijack but no REST lease, resume_without_owner=True."""
    hub = TermHub()
    from unittest.mock import MagicMock

    from provide.terminal.hijack.models import WorkerTermState

    worker_id = "w-resume"
    ws = MagicMock()

    async with hub._lock:
        st = WorkerTermState()
        st.worker_ws = MagicMock()
        st.browsers[ws] = "admin"
        st.hijack_owner = ws
        st.hijack_owner_expires_at = 0  # Expired
        st.events = [{"type": "other_event"}]
        hub._workers[worker_id] = st

    result = await hub.cleanup_browser_disconnect(worker_id, ws, owned_hijack=True)
    assert result["resume_without_owner"] is True


async def test_cleanup_browser_disconnect_no_resume_on_expired_event() -> None:
    """If last event is hijack_owner_expired, don't resume."""
    hub = TermHub()
    from unittest.mock import MagicMock

    from provide.terminal.hijack.models import WorkerTermState

    worker_id = "w-no-resume"
    ws = MagicMock()

    async with hub._lock:
        st = WorkerTermState()
        st.worker_ws = MagicMock()
        st.browsers[ws] = "admin"
        st.hijack_owner = ws
        st.hijack_owner_expires_at = 0
        st.events = [{"type": "hijack_owner_expired"}]
        hub._workers[worker_id] = st

    result = await hub.cleanup_browser_disconnect(worker_id, ws, owned_hijack=True)
    assert result["resume_without_owner"] is False


async def test_cleanup_browser_disconnect_no_resume_on_lease_expired_event() -> None:
    """If last event is hijack_lease_expired, don't resume."""
    hub = TermHub()
    from unittest.mock import MagicMock

    from provide.terminal.hijack.models import WorkerTermState

    worker_id = "w-no-resume-lease"
    ws = MagicMock()

    async with hub._lock:
        st = WorkerTermState()
        st.worker_ws = MagicMock()
        st.browsers[ws] = "admin"
        st.hijack_owner = ws
        st.hijack_owner_expires_at = 0
        st.events = [{"type": "hijack_lease_expired"}]
        hub._workers[worker_id] = st

    result = await hub.cleanup_browser_disconnect(worker_id, ws, owned_hijack=True)
    assert result["resume_without_owner"] is False


# Precision mutation-killing tests for role logic
async def test_can_send_input_open_mode_roles_exact() -> None:
    """can_send_input in open mode: only 'operator' and 'admin', not 'viewer'."""
    from unittest.mock import MagicMock

    from provide.terminal.hijack.models import WorkerTermState

    hub = TermHub()
    st = WorkerTermState()
    st.input_mode = "open"

    # Operator can send
    ws_op = MagicMock()
    st.browsers[ws_op] = "operator"
    assert hub.can_send_input(st, ws_op)
    # Viewer cannot send (critical: 'in' operator)
    ws_viewer = MagicMock()
    st.browsers[ws_viewer] = "viewer"
    assert not hub.can_send_input(st, ws_viewer)
