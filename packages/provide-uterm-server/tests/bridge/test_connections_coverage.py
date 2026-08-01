#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Coverage tests for hijack/hub/connections.py — eviction and set_worker_hello_mode branches."""

from __future__ import annotations

import pytest

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.hub import connections as _conn_module

# ---------------------------------------------------------------------------
# allow_rest_send_for — LRU eviction (lines 93-94)
# ---------------------------------------------------------------------------


def test_allow_rest_send_for_evicts_on_overflow() -> None:
    """Lines 93-94: when _rest_send_per_client reaches max, oldest half are evicted."""
    hub = TermHub()
    cap = _conn_module._REST_CLIENT_CACHE_MAX

    # Fill dict just to the cap by bypassing the public method (avoids rate limit logic).
    from provide.uterm.server.bridge.ratelimit import TokenBucket

    hub.limiter.rest_send_per_client = {f"c{i}": TokenBucket(1) for i in range(cap)}

    # One more call should trigger eviction path
    result = hub.allow_rest_send_for("new-client")
    assert isinstance(result, bool)
    # Dict shrunk by eviction then grew by 1 → total = cap - (cap//2) + 1
    expected = cap - cap // 2 + 1
    assert len(hub.limiter.rest_send_per_client) == expected


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
        from provide.uterm.server.bridge.models import WorkerTermState

        st = WorkerTermState()
        hub.registry._workers[worker_id] = st

    # Activate a hijack lease
    import time

    from provide.uterm.server.bridge.models import HijackSession

    now = time.time()
    async with hub._lock:
        hub.registry._workers[worker_id].hijack_session = HijackSession(
            hijack_id="test-hid",
            owner="alice",
            acquired_at=now,
            lease_expires_at=now + 60,
            last_heartbeat=now,
        )

    # Attempting to switch to open while hijack is active should be blocked
    result = await hub.set_worker_hello_mode(worker_id, "open")
    assert result is False
    assert hub.registry._workers[worker_id].input_mode == "hijack"


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
        from provide.uterm.server.bridge.models import WorkerTermState

        hub.registry._workers[worker_id] = WorkerTermState()

    result = await hub.set_worker_hello_mode(worker_id, "open")
    assert result is True
    assert hub.registry._workers[worker_id].input_mode == "open"


# ---------------------------------------------------------------------------
# force_release_hijack — with REST-style hijack_session (lines 265-267)
# ---------------------------------------------------------------------------


async def test_force_release_hijack_clears_rest_session() -> None:
    """Lines 265-267: force_release clears hijack_session and reports owner."""
    hub = TermHub()
    worker_id = "w-force-rest"
    async with hub._lock:
        import time

        from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

        now = time.time()
        st = WorkerTermState()
        st.hijack_session = HijackSession(
            hijack_id="force-hid",
            owner="bob",
            acquired_at=now,
            lease_expires_at=now + 60,
            last_heartbeat=now,
        )
        hub.registry._workers[worker_id] = st

    result = await hub.force_release_hijack(worker_id)
    assert result is True
    assert hub.registry._workers[worker_id].hijack_session is None


# ---------------------------------------------------------------------------
# allow_rest_acquire_for — eviction and both-bucket-check
# ---------------------------------------------------------------------------


def test_allow_rest_acquire_for_evicts_on_overflow() -> None:
    """LRU eviction for acquire bucket."""
    hub = TermHub()
    cap = _conn_module._REST_CLIENT_CACHE_MAX
    from provide.uterm.server.bridge.ratelimit import TokenBucket

    hub.limiter.rest_acquire_per_client = {f"c{i}": TokenBucket(1) for i in range(cap)}

    result = hub.allow_rest_acquire_for("new-client")
    assert isinstance(result, bool)
    expected = cap - cap // 2 + 1
    assert len(hub.limiter.rest_acquire_per_client) == expected


def test_allow_rest_acquire_for_checks_both_buckets() -> None:
    """Global bucket allow() must pass for positive result."""
    hub = TermHub()
    from unittest.mock import MagicMock

    hub.limiter.rest_acquire_bucket = MagicMock()
    hub.limiter.rest_acquire_bucket.allow.return_value = False
    result = hub.allow_rest_acquire_for("client1")
    assert result is False


def test_allow_rest_send_for_checks_both_buckets() -> None:
    """Global send bucket allow() must pass for positive result."""
    hub = TermHub()
    from unittest.mock import MagicMock

    hub.limiter.rest_send_bucket = MagicMock()
    hub.limiter.rest_send_bucket.allow.return_value = False
    result = hub.allow_rest_send_for("client1")
    assert result is False


# ---------------------------------------------------------------------------
# register_worker — clearing hijack state branches
# ---------------------------------------------------------------------------


# timeout(10): register_worker's fenced re-read loop never suspends, so the
# register_worker__mutmut_40/_41/_42 infinite-retry mutants spin without yielding;
# a short SIGALRM bound makes this test FAIL fast under those mutants (a kill)
# instead of tripping mutmut's wall-clock limit (recorded as a bad `timeout`).
@pytest.mark.timeout(10)
async def test_register_worker_clears_all_hijack_fields() -> None:
    """When prev_was_hijacked, all three hijack fields are cleared."""
    hub = TermHub()
    worker_id = "w-clear"
    import time
    from unittest.mock import MagicMock

    from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

    # Pre-populate with expired hijack state (use monotonic for lease comparison)
    async with hub._lock:
        now = time.monotonic()
        st = WorkerTermState()
        st.hijack_session = HijackSession(
            hijack_id="test",
            owner="alice",
            acquired_at=now,
            lease_expires_at=now - 1,
            last_heartbeat=now,
        )
        st.hijack_owner = MagicMock()
        st.hijack_owner_expires_at = now - 1
        hub.registry._workers[worker_id] = st

    # Register should clear expired hijack
    ws = MagicMock()
    result = await hub.register_worker(worker_id, ws)
    assert result is True
    async with hub._lock:
        st = hub.registry._workers[worker_id]
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
        from provide.uterm.server.bridge.models import WorkerTermState

        st = WorkerTermState()
        st.worker_ws = ws1
        hub.registry._workers[worker_id] = st

    result = await hub.is_active_worker(worker_id, ws2)
    assert result is False


async def test_is_active_worker_returns_true_on_ws_match() -> None:
    """Return True if st.worker_ws matches the provided WebSocket."""
    hub = TermHub()
    worker_id = "w-match"
    from unittest.mock import MagicMock

    ws = MagicMock()
    async with hub._lock:
        from provide.uterm.server.bridge.models import WorkerTermState

        st = WorkerTermState()
        st.worker_ws = ws
        hub.registry._workers[worker_id] = st

    result = await hub.is_active_worker(worker_id, ws)
    assert result is True


# ---------------------------------------------------------------------------
# can_send_input — open mode role check
# ---------------------------------------------------------------------------


async def test_can_send_input_open_mode_viewer_denied() -> None:
    """In open mode, viewer role cannot send input."""
    hub = TermHub()
    from unittest.mock import MagicMock

    from provide.uterm.server.bridge.models import WorkerTermState

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

    from provide.uterm.server.bridge.models import WorkerTermState

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

    from provide.uterm.server.bridge.models import WorkerTermState

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

    from provide.uterm.server.bridge.models import WorkerTermState

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

    from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

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
        hub.registry._workers[worker_id] = st

    result = await hub.cleanup_browser_disconnect(worker_id, ws_owner, False)
    assert result["was_owner"] is True
    assert result["rest_still_active"] is True


async def test_cleanup_browser_disconnect_not_owner_triggers_on_worker_empty() -> None:
    """When last browser disconnects, on_worker_empty callback is invoked."""
    hub = TermHub()
    from unittest.mock import MagicMock

    from provide.uterm.server.bridge.models import WorkerTermState

    worker_id = "w-empty"
    ws = MagicMock()

    async with hub._lock:
        st = WorkerTermState()
        st.browsers[ws] = "viewer"
        hub.registry._workers[worker_id] = st

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

    from provide.uterm.server.bridge.models import WorkerTermState

    worker_id = "w-resume"
    ws = MagicMock()

    async with hub._lock:
        st = WorkerTermState()
        st.worker_ws = MagicMock()
        st.browsers[ws] = "admin"
        st.hijack_owner = ws
        st.hijack_owner_expires_at = 0  # Expired
        st.events = [{"type": "other_event"}]
        hub.registry._workers[worker_id] = st

    result = await hub.cleanup_browser_disconnect(worker_id, ws, owned_hijack=True)
    assert result["resume_without_owner"] is True


async def test_cleanup_browser_disconnect_no_resume_on_expired_event() -> None:
    """If last event is hijack_owner_expired, don't resume."""
    hub = TermHub()
    from unittest.mock import MagicMock

    from provide.uterm.server.bridge.models import WorkerTermState

    worker_id = "w-no-resume"
    ws = MagicMock()

    async with hub._lock:
        st = WorkerTermState()
        st.worker_ws = MagicMock()
        st.browsers[ws] = "admin"
        st.hijack_owner = ws
        st.hijack_owner_expires_at = 0
        st.events = [{"type": "hijack_owner_expired"}]
        hub.registry._workers[worker_id] = st

    result = await hub.cleanup_browser_disconnect(worker_id, ws, owned_hijack=True)
    assert result["resume_without_owner"] is False


async def test_cleanup_browser_disconnect_no_resume_on_lease_expired_event() -> None:
    """If last event is hijack_lease_expired, don't resume."""
    hub = TermHub()
    from unittest.mock import MagicMock

    from provide.uterm.server.bridge.models import WorkerTermState

    worker_id = "w-no-resume-lease"
    ws = MagicMock()

    async with hub._lock:
        st = WorkerTermState()
        st.worker_ws = MagicMock()
        st.browsers[ws] = "admin"
        st.hijack_owner = ws
        st.hijack_owner_expires_at = 0
        st.events = [{"type": "hijack_lease_expired"}]
        hub.registry._workers[worker_id] = st

    result = await hub.cleanup_browser_disconnect(worker_id, ws, owned_hijack=True)
    assert result["resume_without_owner"] is False


# Precision mutation-killing tests for role logic
async def test_can_send_input_open_mode_roles_exact() -> None:
    """can_send_input in open mode: only 'operator' and 'admin', not 'viewer'."""
    from unittest.mock import MagicMock

    from provide.uterm.server.bridge.models import WorkerTermState

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


# ---------------------------------------------------------------------------
# worker_hello vs. an explicit mode decision
# ---------------------------------------------------------------------------


async def test_a_hello_may_set_the_mode_when_nobody_has_decided_one() -> None:
    """The ordinary case, and why the guard cannot simply refuse every lowering.

    ``WorkerTermState.input_mode`` defaults to ``hijack``, so a freshly
    registered worker is already in hijack mode. A rule that refused any
    hello lowering hijack to open would therefore refuse *every* worker that
    announces open — which is most of them.
    """
    hub = TermHub()
    worker_id = "w-hello-undecided"
    async with hub._lock:
        from provide.uterm.server.bridge.models import WorkerTermState

        hub.registry._workers[worker_id] = WorkerTermState()

    assert await hub.set_worker_hello(worker_id, "open") is True
    assert hub.registry.get(worker_id).input_mode == "open"


async def test_a_hello_cannot_undo_an_explicit_mode_decision() -> None:
    """Once a caller has set the mode, a worker's hello may not lower it.

    This is the window the lease-only guard left open: an operator sets
    ``hijack`` and then acquires, and a hello arriving between those two steps
    reverted the mode — so the acquire was refused for being in open mode, a
    message that says nothing about why, and the operator's only clue was a
    failure that looked like their own mistake.
    """
    hub = TermHub()
    worker_id = "w-hello-decided"
    async with hub._lock:
        from provide.uterm.server.bridge.models import WorkerTermState

        hub.registry._workers[worker_id] = WorkerTermState()

    ok, _ = await hub.set_input_mode(worker_id, "hijack")
    assert ok is True

    assert await hub.set_worker_hello(worker_id, "open") is False
    assert hub.registry.get(worker_id).input_mode == "hijack"


async def test_a_hello_may_still_raise_over_a_decision() -> None:
    """The guard is one-directional. A worker announcing ``hijack`` is telling the
    hub something it does not otherwise know — that automation is driving the
    session — so raising is never refused."""
    hub = TermHub()
    worker_id = "w-hello-raise-over"
    async with hub._lock:
        from provide.uterm.server.bridge.models import WorkerTermState

        hub.registry._workers[worker_id] = WorkerTermState()

    ok, _ = await hub.set_input_mode(worker_id, "open")
    assert ok is True

    assert await hub.set_worker_hello(worker_id, "hijack") is True
    assert hub.registry.get(worker_id).input_mode == "hijack"


async def test_a_decision_survives_a_worker_reconnect() -> None:
    """The point of holding this on the worker state rather than the connection:
    registry state outlives a worker socket, so an operator's decision is not
    undone by a reconnect."""
    hub = TermHub()
    worker_id = "w-hello-reconnect"
    async with hub._lock:
        from provide.uterm.server.bridge.models import WorkerTermState

        hub.registry._workers[worker_id] = WorkerTermState()

    ok, _ = await hub.set_input_mode(worker_id, "hijack")
    assert ok is True

    # Two reconnects in a row, each announcing what the worker booted with.
    assert await hub.set_worker_hello(worker_id, "open") is False
    assert await hub.set_worker_hello(worker_id, "open") is False
    assert hub.registry.get(worker_id).input_mode == "hijack"


async def test_deciding_open_is_a_decision_too() -> None:
    """An operator returning a session to open has decided that as much as
    deciding hijack, so a later hello cannot be refused for agreeing with it."""
    hub = TermHub()
    worker_id = "w-hello-open-decision"
    async with hub._lock:
        from provide.uterm.server.bridge.models import WorkerTermState

        hub.registry._workers[worker_id] = WorkerTermState()

    ok, _ = await hub.set_input_mode(worker_id, "open")
    assert ok is True

    assert await hub.set_worker_hello(worker_id, "open") is True
    assert hub.registry.get(worker_id).input_mode == "open"


async def test_a_hello_may_lower_the_mode_its_own_hello_raised() -> None:
    """The guard protects the operator's *value*, and stops the moment a hello
    overrides it.

    An operator returns a session to ``open``; the worker then announces
    ``hijack``, which is allowed to raise over the decision. At that point the
    mode on the state is the worker's, not the operator's — so the worker's
    next hello, announcing ``open`` again, has to be honoured. Holding the flag
    past the override stranded the session in ``hijack`` permanently, with both
    the operator and the worker asking for ``open`` and neither able to get it.
    """
    hub = TermHub()
    worker_id = "w-hello-relower"
    async with hub._lock:
        from provide.uterm.server.bridge.models import WorkerTermState

        hub.registry._workers[worker_id] = WorkerTermState()

    ok, _ = await hub.set_input_mode(worker_id, "open")
    assert ok is True

    assert await hub.set_worker_hello(worker_id, "hijack") is True
    assert hub.registry.get(worker_id).input_mode_set_by_operator is False

    assert await hub.set_worker_hello(worker_id, "open") is True
    assert hub.registry.get(worker_id).input_mode == "open"


async def test_a_hello_that_changes_nothing_leaves_the_decision_standing() -> None:
    """Only an override clears the decision — agreeing with it does not.

    A worker that reconnects and announces the mode the operator already chose
    must not thereby buy itself permission to lower it on the next hello.
    """
    hub = TermHub()
    worker_id = "w-hello-agrees"
    async with hub._lock:
        from provide.uterm.server.bridge.models import WorkerTermState

        hub.registry._workers[worker_id] = WorkerTermState()

    ok, _ = await hub.set_input_mode(worker_id, "hijack")
    assert ok is True

    assert await hub.set_worker_hello(worker_id, "hijack") is True
    assert hub.registry.get(worker_id).input_mode_set_by_operator is True

    assert await hub.set_worker_hello(worker_id, "open") is False
    assert hub.registry.get(worker_id).input_mode == "hijack"
