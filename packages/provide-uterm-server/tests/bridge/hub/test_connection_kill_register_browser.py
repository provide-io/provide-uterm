#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ConnectionManager.register_browser & friends.

Targets in ``provide.uterm.server.bridge.hub.connection``:

* :meth:`ConnectionManager.register_browser` (64 mutants)
* :meth:`ConnectionManager._rollback_browser_quota` (6 mutants)
* :meth:`ConnectionManager._browser_principal_subject_id`
* :meth:`ConnectionManager.activate_browser_broadcasts` (7 mutants)

Every observable is pinned: the full returned dict (all six keys and their
exact values), per-principal quota gate mutations (``_principal_browser_counts``
+ ``_ws_principal``), the 1008 ``WebSocketException`` at-limit path, exempt
principals (None / anonymous / empty), resume-token minting + storage in
``_ws_to_resume_token``, ``defer_broadcast`` -> ``_startup_pending_browsers``,
the snapshot-redaction path via ``_output_policy_gate``, the rollback decrement
on a mid-setup raise, and the ``browser.register`` span + ``EVENT_SESSION_REGISTERED``
log line.

Harness mirrors test_connections_coverage.py: a FRESH ``TermHub()`` per test,
worker state seeded under ``hub._lock``, ``ws`` is a ``MagicMock`` with
``send_text``/``close`` as ``AsyncMock``.
"""

from __future__ import annotations

import asyncio
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocketException

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.hub.ext import EVENT_SESSION_REGISTERED
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------


def _make_ws() -> MagicMock:
    """A browser WebSocket mock with the async I/O surface stubbed."""
    ws = MagicMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    return ws


def _ws_with_principal(subject_id: str) -> MagicMock:
    """A ws whose ``state.uterm_principal.subject_id`` is *subject_id* exactly."""
    ws = _make_ws()
    principal = types.SimpleNamespace(subject_id=subject_id)
    ws.state = types.SimpleNamespace(uterm_principal=principal)
    return ws


def _ws_no_principal() -> MagicMock:
    """A ws whose ``state`` has no ``uterm_principal`` attribute (-> exempt)."""
    ws = _make_ws()
    ws.state = types.SimpleNamespace()
    return ws


async def _seed_worker(hub: TermHub, worker_id: str, st: WorkerTermState) -> None:
    async with hub._lock:
        hub.registry._workers[worker_id] = st


class _FakeResumeStore:
    """Minimal resume-token store: create() returns a fixed token."""

    def __init__(self, token: str = "RESUME-TOK") -> None:  # noqa: S107 - test stub token, not a secret
        self.token = token
        self.create_calls: list[tuple[str, str, float]] = []

    async def create(self, worker_id: str, role: str, ttl_s: float) -> str:
        self.create_calls.append((worker_id, role, ttl_s))
        return self.token

    async def mark_hijack_owner(self, token: str, owned: bool) -> None:  # pragma: no cover - unused here
        pass


# ===========================================================================
# _browser_principal_subject_id
# ===========================================================================


class TestBrowserPrincipalSubjectId:
    def test_no_state_returns_none(self) -> None:
        """No ``state`` attribute at all -> None (exempt)."""
        ws = object()  # bare object: getattr(ws, "state", None) is None
        assert TermHub().connection_mgr._browser_principal_subject_id(ws) is None

    def test_no_uterm_principal_returns_none(self) -> None:
        """state present but no uterm_principal -> None."""
        ws = _ws_no_principal()
        assert TermHub().connection_mgr._browser_principal_subject_id(ws) is None

    def test_anonymous_subject_returns_none(self) -> None:
        """subject_id == 'anonymous' is exempt -> None."""
        ws = _ws_with_principal("anonymous")
        assert TermHub().connection_mgr._browser_principal_subject_id(ws) is None

    def test_empty_subject_returns_none(self) -> None:
        """Empty subject_id is exempt -> None."""
        ws = _ws_with_principal("")
        assert TermHub().connection_mgr._browser_principal_subject_id(ws) is None

    def test_concrete_subject_returns_id(self) -> None:
        """A concrete, non-anonymous subject_id is returned verbatim."""
        ws = _ws_with_principal("alice")
        assert TermHub().connection_mgr._browser_principal_subject_id(ws) == "alice"

    def test_principal_missing_subject_id_attr_returns_none(self) -> None:
        """Principal object without subject_id attr -> '' -> None."""
        ws = _make_ws()
        ws.state = types.SimpleNamespace(uterm_principal=object())
        assert TermHub().connection_mgr._browser_principal_subject_id(ws) is None


# ===========================================================================
# register_browser — returned dict (all six keys/values)
# ===========================================================================


class TestRegisterBrowserReturnDict:
    async def test_baseline_dict_no_hijack_worker_offline(self) -> None:
        """Worker offline, no hijack, hijack mode: pins every dict key/value."""
        hub = TermHub()
        st = WorkerTermState()
        st.input_mode = "hijack"
        st.last_snapshot = None
        await _seed_worker(hub, "w1", st)
        ws = _ws_no_principal()

        result = await hub.register_browser("w1", ws, "viewer")
        assert result == {
            "is_hijacked": False,
            "hijacked_by_me": False,
            "worker_online": False,
            "input_mode": "hijack",
            "initial_snapshot": None,
            "resume_token": None,
        }

    async def test_worker_online_true_when_worker_ws_set(self) -> None:
        """worker_online mirrors st.worker_ws is not None (True case)."""
        hub = TermHub()
        st = WorkerTermState()
        st.worker_ws = _make_ws()
        await _seed_worker(hub, "w1", st)
        ws = _ws_no_principal()

        result = await hub.register_browser("w1", ws, "viewer")
        assert result["worker_online"] is True

    async def test_input_mode_passed_through(self) -> None:
        """input_mode reflects st.input_mode exactly (open here, not hijack)."""
        hub = TermHub()
        st = WorkerTermState()
        st.input_mode = "open"
        await _seed_worker(hub, "w1", st)
        ws = _ws_no_principal()

        result = await hub.register_browser("w1", ws, "operator")
        assert result["input_mode"] == "open"

    async def test_initial_snapshot_passed_through_when_no_gate(self) -> None:
        """initial_snapshot is the stored last_snapshot when no output gate."""
        hub = TermHub()
        snap = {"type": "snapshot", "screen": "hello"}
        st = WorkerTermState()
        st.last_snapshot = snap
        await _seed_worker(hub, "w1", st)
        ws = _ws_no_principal()

        result = await hub.register_browser("w1", ws, "viewer")
        # Same object identity (no gate -> not copied/redacted)
        assert result["initial_snapshot"] is snap

    async def test_is_hijacked_true_reflects_hub_predicate(self) -> None:
        """is_hijacked mirrors hub.is_hijacked(st): True when a REST lease active."""
        hub = TermHub()
        st = WorkerTermState()
        import time

        now = time.monotonic()
        st.hijack_session = HijackSession(
            hijack_id="h1",
            owner="bob",
            acquired_at=now,
            lease_expires_at=now + 600,
            last_heartbeat=now,
        )
        await _seed_worker(hub, "w1", st)
        ws = _ws_no_principal()

        result = await hub.register_browser("w1", ws, "viewer")
        assert result["is_hijacked"] is True
        # REST lease is not a dashboard WS lease owned by this ws.
        assert result["hijacked_by_me"] is False

    async def test_hijacked_by_me_true_when_dashboard_owner_is_ws(self) -> None:
        """hijacked_by_me True iff dashboard hijack active AND owner is this ws."""
        hub = TermHub()
        import time

        st = WorkerTermState()
        await _seed_worker(hub, "w1", st)
        ws = _ws_no_principal()
        # Make ws the active dashboard owner before registering.
        async with hub._lock:
            st.hijack_owner = ws
            st.hijack_owner_expires_at = time.monotonic() + 600

        result = await hub.register_browser("w1", ws, "admin")
        assert result["hijacked_by_me"] is True
        assert result["is_hijacked"] is True

    async def test_hijacked_by_me_false_when_owner_is_other_ws(self) -> None:
        """hijacked_by_me False when dashboard owner is a different ws."""
        hub = TermHub()
        import time

        st = WorkerTermState()
        other = _make_ws()
        await _seed_worker(hub, "w1", st)
        async with hub._lock:
            st.hijack_owner = other
            st.hijack_owner_expires_at = time.monotonic() + 600
        ws = _ws_no_principal()

        result = await hub.register_browser("w1", ws, "viewer")
        assert result["hijacked_by_me"] is False

    async def test_registers_browser_role_in_state(self) -> None:
        """The ws is recorded in st.browsers with the exact role."""
        hub = TermHub()
        st = WorkerTermState()
        await _seed_worker(hub, "w1", st)
        ws = _ws_no_principal()

        await hub.register_browser("w1", ws, "operator")
        assert hub.registry._workers["w1"].browsers[ws] == "operator"

    async def test_creates_worker_state_when_absent(self) -> None:
        """register_browser setdefaults a WorkerTermState for an unknown worker."""
        hub = TermHub()
        ws = _ws_no_principal()
        assert "wnew" not in hub.registry._workers

        result = await hub.register_browser("wnew", ws, "viewer")
        assert "wnew" in hub.registry._workers
        assert hub.registry._workers["wnew"].browsers[ws] == "viewer"
        # Fresh state defaults: offline, hijack mode, no snapshot.
        assert result == {
            "is_hijacked": False,
            "hijacked_by_me": False,
            "worker_online": False,
            "input_mode": "hijack",
            "initial_snapshot": None,
            "resume_token": None,
        }


# ===========================================================================
# register_browser — per-principal quota gate
# ===========================================================================


class TestRegisterBrowserQuota:
    async def test_exempt_principal_not_counted(self) -> None:
        """A ws with no principal does NOT touch _principal_browser_counts/_ws_principal."""
        hub = TermHub()
        await _seed_worker(hub, "w1", WorkerTermState())
        ws = _ws_no_principal()

        await hub.register_browser("w1", ws, "viewer")
        assert hub._principal_browser_counts == {}
        assert ws not in hub._ws_principal

    async def test_anonymous_principal_not_counted(self) -> None:
        """An 'anonymous' principal is exempt from the quota counter."""
        hub = TermHub()
        await _seed_worker(hub, "w1", WorkerTermState())
        ws = _ws_with_principal("anonymous")

        await hub.register_browser("w1", ws, "viewer")
        assert hub._principal_browser_counts == {}
        assert ws not in hub._ws_principal

    async def test_concrete_principal_increments_count_and_maps_ws(self) -> None:
        """Under-limit concrete principal: count -> 1 and _ws_principal[ws] set."""
        hub = TermHub()
        await _seed_worker(hub, "w1", WorkerTermState())
        ws = _ws_with_principal("alice")

        await hub.register_browser("w1", ws, "viewer")
        assert hub._principal_browser_counts["alice"] == 1
        assert hub._ws_principal[ws] == "alice"

    async def test_second_connection_increments_to_two(self) -> None:
        """A second connection for the same principal increments to 2 (current+1)."""
        hub = TermHub(max_connections_per_principal=5)
        await _seed_worker(hub, "w1", WorkerTermState())
        ws1 = _ws_with_principal("alice")
        ws2 = _ws_with_principal("alice")

        await hub.register_browser("w1", ws1, "viewer")
        await hub.register_browser("w1", ws2, "viewer")
        assert hub._principal_browser_counts["alice"] == 2
        assert hub._ws_principal[ws1] == "alice"
        assert hub._ws_principal[ws2] == "alice"

    async def test_at_limit_raises_1008_and_does_not_increment(self) -> None:
        """At the limit: WebSocketException(1008), count unchanged, ws unmapped."""
        hub = TermHub(max_connections_per_principal=1)
        await _seed_worker(hub, "w1", WorkerTermState())
        # Pre-seed the principal at exactly the limit.
        hub._principal_browser_counts["alice"] = 1
        ws = _ws_with_principal("alice")

        with pytest.raises(WebSocketException) as ei:
            await hub.register_browser("w1", ws, "viewer")
        assert ei.value.code == 1008
        assert ei.value.reason == "too many connections"
        # No increment past the cap, and the rejected ws is not recorded.
        assert hub._principal_browser_counts["alice"] == 1
        assert ws not in hub._ws_principal
        # Rejected ws never landed in the browsers map.
        assert ws not in hub.registry._workers["w1"].browsers

    async def test_over_limit_also_raises(self) -> None:
        """current > limit also triggers 1008 (>= comparison, not ==)."""
        hub = TermHub(max_connections_per_principal=2)
        await _seed_worker(hub, "w1", WorkerTermState())
        hub._principal_browser_counts["alice"] = 3  # already over
        ws = _ws_with_principal("alice")

        with pytest.raises(WebSocketException) as ei:
            await hub.register_browser("w1", ws, "viewer")
        assert ei.value.code == 1008
        assert hub._principal_browser_counts["alice"] == 3

    async def test_just_under_limit_allowed(self) -> None:
        """current == limit-1 is allowed and increments to limit."""
        hub = TermHub(max_connections_per_principal=3)
        await _seed_worker(hub, "w1", WorkerTermState())
        hub._principal_browser_counts["alice"] = 2  # one below cap of 3
        ws = _ws_with_principal("alice")

        await hub.register_browser("w1", ws, "viewer")
        assert hub._principal_browser_counts["alice"] == 3
        assert hub._ws_principal[ws] == "alice"


# ===========================================================================
# register_browser — resume token minting
# ===========================================================================


class TestRegisterBrowserResumeToken:
    async def test_no_resume_store_token_is_none(self) -> None:
        """No resume store -> resume_token None and _ws_to_resume_token untouched."""
        hub = TermHub()  # resume_store defaults to None
        await _seed_worker(hub, "w1", WorkerTermState())
        ws = _ws_no_principal()

        result = await hub.register_browser("w1", ws, "viewer")
        assert result["resume_token"] is None
        assert ws not in hub._ws_to_resume_token

    async def test_resume_store_present_mints_and_stores_token(self) -> None:
        """resume_store present -> token minted, returned, and stored in map."""
        store = _FakeResumeStore("TOK-XYZ")
        hub = TermHub(resume_store=store, resume_ttl_s=123)
        await _seed_worker(hub, "w1", WorkerTermState())
        ws = _ws_no_principal()

        result = await hub.register_browser("w1", ws, "operator")
        assert result["resume_token"] == "TOK-XYZ"
        assert hub._ws_to_resume_token[ws] == "TOK-XYZ"
        # create() invoked with exactly (worker_id, role, ttl).
        assert store.create_calls == [("w1", "operator", hub._resume_ttl_s)]

    async def test_binding_arms_the_detach_latch_for_the_minted_token(self) -> None:
        """The mint must go through ``_bind_resume_token_locked``, not a raw
        ``_ws_to_resume_token`` write.

        The latch is what lets a reconnecting browser order itself behind THIS
        socket's disconnect bookkeeping; a bind that skips it silently restores
        the lost-reclaim race. Pinned as armed-but-unset: an already-set latch
        would release a waiter immediately and be just as broken.
        """
        store = _FakeResumeStore("TOK-LATCH")
        hub = TermHub(resume_store=store)
        await _seed_worker(hub, "w1", WorkerTermState())
        ws = _ws_no_principal()

        await hub.register_browser("w1", ws, "operator")
        latch = hub._resume_token_detached["TOK-LATCH"]
        assert isinstance(latch, asyncio.Event)
        assert latch.is_set() is False

    async def test_no_resume_store_arms_no_latch(self) -> None:
        """Without a store nothing is minted, so no latch is armed."""
        hub = TermHub()
        await _seed_worker(hub, "w1", WorkerTermState())

        await hub.register_browser("w1", _ws_no_principal(), "viewer")
        assert hub._resume_token_detached == {}


# ===========================================================================
# register_browser — defer_broadcast
# ===========================================================================


class TestRegisterBrowserDeferBroadcast:
    async def test_defer_broadcast_adds_to_startup_pending(self) -> None:
        """defer_broadcast=True adds ws to _startup_pending_browsers."""
        hub = TermHub()
        await _seed_worker(hub, "w1", WorkerTermState())
        ws = _ws_no_principal()

        await hub.register_browser("w1", ws, "viewer", defer_broadcast=True)
        assert ws in hub._startup_pending_browsers

    async def test_no_defer_does_not_add_to_startup_pending(self) -> None:
        """defer_broadcast=False (default) leaves _startup_pending_browsers empty."""
        hub = TermHub()
        await _seed_worker(hub, "w1", WorkerTermState())
        ws = _ws_no_principal()

        await hub.register_browser("w1", ws, "viewer")
        assert ws not in hub._startup_pending_browsers
        assert hub._startup_pending_browsers == set()


# ===========================================================================
# register_browser — snapshot redaction path
# ===========================================================================


class TestRegisterBrowserRedaction:
    async def test_redaction_applied_when_gate_set_and_snapshot_present(self) -> None:
        """With an output gate and a snapshot, redact_snapshot_for_recipient is used."""
        hub = TermHub()
        snap = {"type": "snapshot", "screen": "secret"}
        st = WorkerTermState()
        st.last_snapshot = snap
        await _seed_worker(hub, "w1", st)
        ws = _ws_no_principal()

        # Activate the gate sentinel and stub the redactor to a known copy.
        hub._output_policy_gate = object()
        redacted = {"type": "snapshot", "screen": "[REDACTED]"}
        hub.redact_snapshot_for_recipient = AsyncMock(return_value=redacted)  # type: ignore[method-assign]

        result = await hub.register_browser("w1", ws, "viewer")
        assert result["initial_snapshot"] == redacted
        hub.redact_snapshot_for_recipient.assert_awaited_once_with("w1", snap, ws)

    async def test_no_redaction_when_gate_none(self) -> None:
        """No gate -> snapshot passes through unredacted, redactor not called."""
        hub = TermHub()  # _output_policy_gate defaults to None
        snap = {"type": "snapshot", "screen": "plain"}
        st = WorkerTermState()
        st.last_snapshot = snap
        await _seed_worker(hub, "w1", st)
        ws = _ws_no_principal()

        hub.redact_snapshot_for_recipient = AsyncMock()  # type: ignore[method-assign]
        result = await hub.register_browser("w1", ws, "viewer")
        assert result["initial_snapshot"] is snap
        hub.redact_snapshot_for_recipient.assert_not_called()

    async def test_no_redaction_when_snapshot_none_even_with_gate(self) -> None:
        """Gate active but snapshot None -> redactor NOT called (guard on snapshot)."""
        hub = TermHub()
        st = WorkerTermState()
        st.last_snapshot = None
        await _seed_worker(hub, "w1", st)
        ws = _ws_no_principal()

        hub._output_policy_gate = object()
        hub.redact_snapshot_for_recipient = AsyncMock()  # type: ignore[method-assign]
        result = await hub.register_browser("w1", ws, "viewer")
        assert result["initial_snapshot"] is None
        hub.redact_snapshot_for_recipient.assert_not_called()


# ===========================================================================
# register_browser — rollback on mid-setup raise
# ===========================================================================


class TestRegisterBrowserRollback:
    async def test_raise_after_increment_rolls_back_count_and_maps(self) -> None:
        """A raise after the quota increment rolls back count, ws-principal, token."""
        store = _FakeResumeStore()

        async def _boom(*_a: Any, **_k: Any) -> str:
            raise RuntimeError("sqlite IO error")

        store.create = _boom  # type: ignore[assignment]
        hub = TermHub(resume_store=store)
        await _seed_worker(hub, "w1", WorkerTermState())
        ws = _ws_with_principal("alice")

        with pytest.raises(RuntimeError, match="sqlite IO error"):
            await hub.register_browser("w1", ws, "viewer")
        # Increment rolled back to nothing (popped to zero).
        assert "alice" not in hub._principal_browser_counts
        assert ws not in hub._ws_principal
        assert ws not in hub._ws_to_resume_token
        # The browser was never recorded in state.
        assert ws not in hub.registry._workers["w1"].browsers

    async def test_raise_after_mint_releases_the_tokens_detach_latch(self) -> None:
        """A raise AFTER the token is minted is a terminal path for the socket.

        The rollback must release the latch it armed, otherwise a browser that
        reconnects with a stale token blocks for the full
        ``RESUME_TOKEN_DETACH_TIMEOUT_S`` bound waiting on a socket that never
        existed.
        """
        store = _FakeResumeStore("TOK-ORPHAN")
        hub = TermHub(resume_store=store)
        await _seed_worker(hub, "w1", WorkerTermState())
        ws = _ws_with_principal("alice")

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("state read exploded")

        # is_hijacked() is read while building initial_state — i.e. after the
        # token has been minted and bound, which is the window under test.
        hub.is_hijacked = _boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="state read exploded"):
            await hub.register_browser("w1", ws, "viewer")

        assert hub._resume_token_detached == {}
        assert ws not in hub._ws_to_resume_token

    async def test_rollback_preserves_other_connections_of_same_principal(self) -> None:
        """Rollback decrements (not zeroes) when the principal has other live conns."""
        store = _FakeResumeStore()
        hub = TermHub(resume_store=store, max_connections_per_principal=10)
        await _seed_worker(hub, "w1", WorkerTermState())

        ws_ok = _ws_with_principal("alice")
        await hub.register_browser("w1", ws_ok, "viewer")
        assert hub._principal_browser_counts["alice"] == 1

        # Now a second connect raises mid-setup; only it should roll back.
        async def _boom(*_a: Any, **_k: Any) -> str:
            raise RuntimeError("boom")

        store.create = _boom  # type: ignore[assignment]
        ws_bad = _ws_with_principal("alice")
        with pytest.raises(RuntimeError, match="boom"):
            await hub.register_browser("w1", ws_bad, "viewer")

        # First connection's slot survives; the failed one's increment is undone.
        assert hub._principal_browser_counts["alice"] == 1
        assert hub._ws_principal[ws_ok] == "alice"
        assert ws_bad not in hub._ws_principal


# ===========================================================================
# _rollback_browser_quota (direct unit tests)
# ===========================================================================


class TestRollbackBrowserQuota:
    def test_pops_resume_token_and_principal_and_zeroes_count(self) -> None:
        """Single-connection principal: rollback pops the count entry entirely."""
        hub = TermHub()
        ws = _make_ws()
        hub._ws_to_resume_token[ws] = "TOK"
        hub._ws_principal[ws] = "alice"
        hub._principal_browser_counts["alice"] = 1

        hub.connection_mgr._rollback_browser_quota(ws)
        assert ws not in hub._ws_to_resume_token
        assert ws not in hub._ws_principal
        assert "alice" not in hub._principal_browser_counts

    def test_pops_the_popped_tokens_latch_and_releases_waiters(self) -> None:
        """The popped token is the one detached — pinned by releasing its latch.

        A rollback that detaches nothing (or detaches some other token) leaves a
        waiter blocked; a rollback that pops without detaching leaks the latch.
        """
        hub = TermHub()
        ws = _make_ws()
        latch = asyncio.Event()
        other = asyncio.Event()
        hub._ws_to_resume_token[ws] = "TOK"
        hub._resume_token_detached["TOK"] = latch
        hub._resume_token_detached["OTHER"] = other

        hub.connection_mgr._rollback_browser_quota(ws)
        assert latch.is_set() is True
        assert "TOK" not in hub._resume_token_detached
        # An unrelated socket's latch is untouched.
        assert other.is_set() is False
        assert hub._resume_token_detached["OTHER"] is other

    def test_no_token_leaves_every_latch_untouched(self) -> None:
        """Nothing bound for ws -> the detach is a no-op, not a blind pop."""
        hub = TermHub()
        latch = asyncio.Event()
        hub._resume_token_detached["TOK"] = latch

        hub.connection_mgr._rollback_browser_quota(_make_ws())
        assert latch.is_set() is False
        assert hub._resume_token_detached == {"TOK": latch}

    def test_decrements_when_remaining_positive(self) -> None:
        """Multi-connection principal: rollback decrements but keeps the entry."""
        hub = TermHub()
        ws = _make_ws()
        hub._ws_principal[ws] = "alice"
        hub._principal_browser_counts["alice"] = 3

        hub.connection_mgr._rollback_browser_quota(ws)
        assert hub._principal_browser_counts["alice"] == 2
        assert ws not in hub._ws_principal

    def test_no_principal_mapping_only_pops_token(self) -> None:
        """ws with no principal mapping: only the resume token is popped."""
        hub = TermHub()
        ws = _make_ws()
        hub._ws_to_resume_token[ws] = "TOK"
        # No _ws_principal entry.
        hub.connection_mgr._rollback_browser_quota(ws)
        assert ws not in hub._ws_to_resume_token
        assert hub._principal_browser_counts == {}

    def test_remaining_exactly_zero_pops_entry(self) -> None:
        """When count was 1, remaining==0 -> entry popped (<= 0 boundary)."""
        hub = TermHub()
        ws = _make_ws()
        hub._ws_principal[ws] = "bob"
        hub._principal_browser_counts["bob"] = 1

        hub.connection_mgr._rollback_browser_quota(ws)
        assert "bob" not in hub._principal_browser_counts

    def test_no_token_no_principal_is_noop(self) -> None:
        """Nothing mapped -> no error, maps remain empty."""
        hub = TermHub()
        ws = _make_ws()
        hub.connection_mgr._rollback_browser_quota(ws)
        assert hub._ws_to_resume_token == {}
        assert hub._ws_principal == {}
        assert hub._principal_browser_counts == {}


# ===========================================================================
# activate_browser_broadcasts
# ===========================================================================


class TestActivateBrowserBroadcasts:
    async def test_discards_pending_when_browser_present(self) -> None:
        """ws is in st.browsers -> removed from _startup_pending_browsers."""
        hub = TermHub()
        st = WorkerTermState()
        ws = _make_ws()
        st.browsers[ws] = "viewer"
        await _seed_worker(hub, "w1", st)
        hub._startup_pending_browsers.add(ws)

        await hub.activate_browser_broadcasts("w1", ws)
        assert ws not in hub._startup_pending_browsers

    async def test_unknown_worker_leaves_pending_set(self) -> None:
        """Unknown worker (st None) -> pending set unchanged."""
        hub = TermHub()
        ws = _make_ws()
        hub._startup_pending_browsers.add(ws)

        await hub.activate_browser_broadcasts("nope", ws)
        assert ws in hub._startup_pending_browsers

    async def test_browser_not_in_state_leaves_pending_set(self) -> None:
        """Worker exists but ws not in st.browsers -> pending set unchanged."""
        hub = TermHub()
        st = WorkerTermState()
        await _seed_worker(hub, "w1", st)
        ws = _make_ws()
        hub._startup_pending_browsers.add(ws)

        await hub.activate_browser_broadcasts("w1", ws)
        assert ws in hub._startup_pending_browsers

    async def test_only_target_ws_discarded(self) -> None:
        """Only the target ws is discarded; other pending browsers remain."""
        hub = TermHub()
        st = WorkerTermState()
        ws = _make_ws()
        other = _make_ws()
        st.browsers[ws] = "viewer"
        await _seed_worker(hub, "w1", st)
        hub._startup_pending_browsers.update({ws, other})

        await hub.activate_browser_broadcasts("w1", ws)
        assert ws not in hub._startup_pending_browsers
        assert other in hub._startup_pending_browsers


# ===========================================================================
# register_browser — observability (span + log)
# ===========================================================================


class TestRegisterBrowserObservability:
    async def test_span_and_log_exact(self) -> None:
        """Pins the browser.register span (name + attrs) and the registered log."""
        hub = TermHub()
        await _seed_worker(hub, "w1", WorkerTermState())
        ws = _ws_no_principal()

        with (
            patch("provide.uterm.server.bridge.hub.connection.logger") as mlog,
            patch("provide.uterm.server.bridge.hub.connection.tracer") as mtr,
        ):
            await hub.register_browser("w1", ws, "operator")

        mtr.start_as_current_span.assert_called_once_with(
            "uterm.browser.register", attributes={"worker_id": "w1", "role": "operator"}
        )
        mlog.info.assert_called_once_with(
            EVENT_SESSION_REGISTERED, worker_id="w1", session_type="browser", role="operator"
        )

    async def test_no_log_on_quota_rejection(self) -> None:
        """A 1008 rejection short-circuits before the registered log line."""
        hub = TermHub(max_connections_per_principal=1)
        await _seed_worker(hub, "w1", WorkerTermState())
        hub._principal_browser_counts["alice"] = 1
        ws = _ws_with_principal("alice")

        with (
            patch("provide.uterm.server.bridge.hub.connection.logger") as mlog,
            patch("provide.uterm.server.bridge.hub.connection.tracer") as mtr,
            pytest.raises(WebSocketException),
        ):
            await hub.register_browser("w1", ws, "viewer")

        # Span still opened (it wraps the whole body), but no registered log.
        mtr.start_as_current_span.assert_called_once_with(
            "uterm.browser.register", attributes={"worker_id": "w1", "role": "viewer"}
        )
        mlog.info.assert_not_called()
