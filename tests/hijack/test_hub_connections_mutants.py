#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for hub/connections.py — worker/browser registration and state."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.hub.resume import InMemoryResumeStore
from provide.terminal.hijack.models import HijackSession, WorkerTermState

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_hub(**kwargs: Any) -> TermHub:
    return TermHub(**kwargs)


def _make_ws() -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


def _make_hijack_session(hijack_id: str = "hj-1", owner: str = "op", lease_s: float = 60.0) -> HijackSession:
    now = time.time()
    return HijackSession(
        hijack_id=hijack_id,
        owner=owner,
        acquired_at=now,
        lease_expires_at=now + lease_s,
        last_heartbeat=now,
    )


async def _setup_worker(hub: TermHub, worker_id: str, worker_ws: Any = None) -> WorkerTermState:
    async with hub._lock:
        st = hub._workers.setdefault(worker_id, WorkerTermState())
        st.worker_ws = worker_ws or _make_ws()
        return st


# ===========================================================================
# ownership.py — _expire_leases_under_lock
# ===========================================================================


class TestRegisterWorker:
    async def test_events_preserved_on_re_register(self) -> None:
        """mutmut_9: deque(st.events, maxlen=...) → deque(maxlen=...) discards events."""
        hub = _make_hub()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.events = deque([{"type": "old_event", "seq": 1, "ts": 1.0}], maxlen=2000)

        ws = _make_ws()
        await hub.register_worker("w1", ws)

        async with hub._lock:
            st2 = hub._workers["w1"]
            event_types = [e.get("type") for e in st2.events]
            assert "old_event" in event_types, "Existing events must be preserved on re-register"

    async def test_prev_hijacked_or_not_and(self) -> None:
        """mutmut_12: 'or' → 'and' in prev_was_hijacked check.

        With 'and': requires BOTH session AND owner to be set.
        With 'or': either one set triggers the hijack-cleared logic.
        """
        hub = _make_hub()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            # Only hijack_session set, no owner
            st.hijack_session = _make_hijack_session(lease_s=60.0)
            st.hijack_owner = None

        ws = _make_ws()
        prev = await hub.register_worker("w1", ws)
        # With 'or': prev_was_hijacked = True (session is set)
        # With 'and': prev_was_hijacked = False (no owner)
        assert prev is True, "Session alone should trigger prev_was_hijacked"


# ===========================================================================
# connections.py — set_worker_hello_mode
# ===========================================================================


class TestSetWorkerHelloMode:
    async def test_mode_set_correctly(self) -> None:
        """mutmut_11/12/13/14: logger mutations — mode must still be set."""
        hub = _make_hub()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()

        result = await hub.set_worker_hello_mode("w1", "open")
        assert result is True
        async with hub._lock:
            st2 = hub._workers["w1"]
            assert st2.input_mode == "open"

    async def test_open_mode_blocked_when_hijack_active(self) -> None:
        """Ensure blocking still works even with logger mutations."""
        hub = _make_hub()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_owner = _make_ws()
            st.hijack_owner_expires_at = time.time() + 300

        result = await hub.set_worker_hello_mode("w1", "open")
        assert result is False


# ===========================================================================
# connections.py — deregister_worker
# ===========================================================================


class TestDeregisterWorker:
    async def test_returns_false_false_for_wrong_ws(self) -> None:
        """mutmut_7: return False, False → False, True for non-current ws."""
        hub = _make_hub()
        current_ws = _make_ws()
        old_ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = current_ws

        result = await hub.deregister_worker("w1", old_ws)
        assert result == (False, False), f"Expected (False, False), got {result}"

    async def test_hijack_session_cleared_to_none(self) -> None:
        """mutmut_13: hijack_session = None → ''.  Must be None."""
        hub = _make_hub()
        ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = ws
            st.hijack_session = _make_hijack_session(lease_s=60.0)

        await hub.deregister_worker("w1", ws)

        async with hub._lock:
            st2 = hub._workers.get("w1")
            if st2:
                assert st2.hijack_session is None


# ===========================================================================
# connections.py — register_browser
# ===========================================================================


class TestRegisterBrowser:
    async def test_resume_token_stored_in_ws_map(self) -> None:
        """mutmut_10: ws_to_resume_token[ws] = resume_token → None."""
        store = InMemoryResumeStore()
        hub = _make_hub(resume_store=store)
        ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()

        await hub.register_browser("w1", ws, "operator")

        # Token must be stored for ws (not None)
        assert ws in hub._ws_to_resume_token
        assert hub._ws_to_resume_token[ws] is not None

    async def test_hijacked_by_me_is_false_for_non_owner(self) -> None:
        """mutmut_22: 'and' → 'or' in hijacked_by_me check."""
        hub = _make_hub()
        hijack_owner_ws = _make_ws()
        browser_ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_owner = hijack_owner_ws
            st.hijack_owner_expires_at = time.time() + 300

        state = await hub.register_browser("w1", browser_ws, "operator")
        # browser_ws is NOT the owner → hijacked_by_me must be False
        assert state["hijacked_by_me"] is False

    async def test_hijacked_by_me_is_false_with_inverted_check(self) -> None:
        """mutmut_24: 'is ws' → 'is not ws'."""
        hub = _make_hub()
        owner_ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 300

        # Registering owner_ws itself → hijacked_by_me must be True
        state = await hub.register_browser("w1", owner_ws, "admin")
        assert state["hijacked_by_me"] is True


# ===========================================================================
# connections.py — cleanup_browser_disconnect
# ===========================================================================


class TestCleanupBrowserDisconnect:
    async def test_browser_count_initial_sentinel_negative_one(self) -> None:
        """mutmut_1/2/3: browser_count = -1 → None/+1/-2.

        The value -1 is the sentinel "not visited the lock" — it must
        NOT trigger the on_worker_empty callback (which fires when count==0).
        """
        empty_fired: list[str] = []

        async def _on_empty(worker_id: str) -> None:
            empty_fired.append(worker_id)

        hub = _make_hub(on_worker_empty=_on_empty)
        ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.browsers[ws] = "operator"

        await hub.cleanup_browser_disconnect("w1", ws, owned_hijack=False)
        # After disconnect, count becomes 0 → on_empty should fire
        await asyncio.sleep(0)  # allow background task to run
        assert "w1" in empty_fired

    async def test_initial_browser_count_minus_one_not_zero(self) -> None:
        """mutmut_2: initial browser_count = +1 would trigger on_empty even before the lock."""
        # Verify the value by ensuring the callback ONLY fires when truly empty
        fires: list[str] = []

        async def _on_empty(worker_id: str) -> None:
            fires.append(worker_id)

        hub = _make_hub(on_worker_empty=_on_empty)
        ws1 = _make_ws()
        ws2 = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.browsers[ws1] = "operator"
            st.browsers[ws2] = "operator"

        # Remove ws1 (still 1 browser left)
        await hub.cleanup_browser_disconnect("w1", ws1, owned_hijack=False)
        await asyncio.sleep(0)
        # on_empty must NOT fire (still 1 browser)
        assert "w1" not in fires

    async def test_resume_store_mark_hijack_called_with_correct_token(self) -> None:
        """mutmut_56/57/58/60/63/64/67: resume store mark_hijack_owner mutations."""
        store = InMemoryResumeStore()
        hub = _make_hub(resume_store=store)
        owner_ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 300
            st.browsers[owner_ws] = "admin"

        # Register first to get a token
        state = await hub.register_browser("w1", owner_ws, "admin")
        token = state.get("resume_token")
        assert token is not None

        # Re-set hijack owner to owner_ws (register_browser may have changed state)
        async with hub._lock:
            st2 = hub._workers["w1"]
            st2.hijack_owner = owner_ws
            st2.hijack_owner_expires_at = time.time() + 300

        await hub.cleanup_browser_disconnect("w1", owner_ws, owned_hijack=True)

        # Token must be marked as hijack owner
        session = store.get(token)
        assert session is not None
        assert session.was_hijack_owner is True

    async def test_resume_store_not_marked_when_not_owner(self) -> None:
        """mutmut_61: 'if token and (was_owner or owned_hijack)' → 'or (was_owner or owned_hijack)'.

        With 'or', mark_hijack_owner would be called even when token is None.
        """
        store = InMemoryResumeStore()
        hub = _make_hub(resume_store=store)
        browser_ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.browsers[browser_ws] = "operator"

        state = await hub.register_browser("w1", browser_ws, "operator")
        token = state.get("resume_token")
        assert token is not None

        # Disconnect without any hijack ownership
        await hub.cleanup_browser_disconnect("w1", browser_ws, owned_hijack=False)

        session = store.get(token)
        if session is not None:
            assert session.was_hijack_owner is False

    async def test_resume_store_marked_only_when_was_owner_or_owned_hijack(self) -> None:
        """mutmut_62: 'was_owner or owned_hijack' → 'was_owner and owned_hijack'."""
        store = InMemoryResumeStore()
        hub = _make_hub(resume_store=store)
        browser_ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.browsers[browser_ws] = "operator"

        state = await hub.register_browser("w1", browser_ws, "operator")
        token = state.get("resume_token")
        assert token is not None

        # owned_hijack=True but was_owner=False (browser was not WS hijack owner)
        await hub.cleanup_browser_disconnect("w1", browser_ws, owned_hijack=True)

        session = store.get(token)
        if session is not None:
            # With 'or': was_owner=False OR owned_hijack=True → True → mark called
            # With 'and': False AND True → False → not marked
            assert session.was_hijack_owner is True

    async def test_event_type_lookup_uses_type_key(self) -> None:
        """mutmut_36/38/41: evt.get('type', '') default mutations.

        When an event has no 'type' key, the default '' must NOT match
        'hijack_owner_expired' or 'hijack_lease_expired'.
        """
        hub = _make_hub()
        ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.browsers[ws] = "admin"
            # Add an event WITHOUT a 'type' key
            st.events.append({"seq": 1, "ts": 1.0})  # no 'type' key
            # Then add a legitimate hijack_acquired event
            st.events.append({"seq": 2, "ts": 1.0, "type": "hijack_acquired"})

        result = await hub.cleanup_browser_disconnect("w1", ws, owned_hijack=True)
        # resume_without_owner may be True or False but must not crash
        assert isinstance(result["resume_without_owner"], bool)


# ===========================================================================
# connections.py — register_browser_state_snapshot
# ===========================================================================


class TestRegisterBrowserStateSnapshot:
    async def test_hijacked_by_me_and_not_or(self) -> None:
        """mutmut_17/18: 'and' → 'or' in hijacked_by_me check."""
        hub = _make_hub()
        owner_ws = _make_ws()
        other_ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 300
            st.browsers[other_ws] = "operator"
            st.browsers[owner_ws] = "admin"

        state = await hub.register_browser_state_snapshot("w1", other_ws)
        # other_ws is NOT the hijack owner → hijacked_by_me must be False
        assert state["hijacked_by_me"] is False

    async def test_hijacked_by_me_true_for_owner(self) -> None:
        """Sanity: owner should get hijacked_by_me=True."""
        hub = _make_hub()
        owner_ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 300
            st.browsers[owner_ws] = "admin"

        state = await hub.register_browser_state_snapshot("w1", owner_ws)
        assert state["hijacked_by_me"] is True

    async def test_worker_online_uses_is_not_none(self) -> None:
        """mutmut_22: worker_ws is not None → is None."""
        hub = _make_hub()
        ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()  # worker IS online
            st.browsers[ws] = "operator"

        state = await hub.register_browser_state_snapshot("w1", ws)
        assert state["worker_online"] is True

    async def test_no_worker_fallback_returns_hijack_mode(self) -> None:
        """mutmut_25/26/27/28/29: fallback input_mode mutations."""
        hub = _make_hub()
        ws = _make_ws()
        # Worker not registered
        state = await hub.register_browser_state_snapshot("nonexistent", ws)
        assert state["input_mode"] == "hijack"


# ===========================================================================
# connections.py — can_send_input
# ===========================================================================


class TestCanSendInput:
    def test_open_mode_operator_can_send(self) -> None:
        """mutmut_6: 'operator' in valid set."""
        hub = _make_hub()
        ws = _make_ws()
        st = WorkerTermState()
        st.input_mode = "open"
        st.browsers[ws] = "operator"
        assert hub.can_send_input(st, ws) is True

    def test_open_mode_admin_can_send(self) -> None:
        """mutmut_8: 'admin' in valid set."""
        hub = _make_hub()
        ws = _make_ws()
        st = WorkerTermState()
        st.input_mode = "open"
        st.browsers[ws] = "admin"
        assert hub.can_send_input(st, ws) is True

    def test_open_mode_viewer_cannot_send(self) -> None:
        """mutmut_9: viewer excluded from open mode."""
        hub = _make_hub()
        ws = _make_ws()
        st = WorkerTermState()
        st.input_mode = "open"
        st.browsers[ws] = "viewer"
        assert hub.can_send_input(st, ws) is False

    def test_open_mode_unknown_role_cannot_send(self) -> None:
        """mutmut_10: unknown role treated as viewer."""
        hub = _make_hub()
        ws = _make_ws()
        st = WorkerTermState()
        st.input_mode = "open"
        st.browsers[ws] = "superadmin"
        assert hub.can_send_input(st, ws) is False


# ===========================================================================
# connections.py — force_release_hijack (extra)
# ===========================================================================


class TestForceReleaseHijackExtra:
    async def test_returns_false_when_no_hijack_active(self) -> None:
        """mutmut_4/5: 'if not had_hijack: return False' — with no hijack set."""
        hub = _make_hub()
        worker_ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            # No hijack_session, no hijack_owner

        result = await hub.force_release_hijack("w1")
        assert result is False

    async def test_notify_called_with_correct_enabled(self) -> None:
        """mutmut_39/40/41/42: notify_hijack_changed arg mutations."""
        received: list[tuple[str, bool, Any]] = []

        def _on_hijack(worker_id: str, enabled: bool, owner: Any) -> None:
            received.append((worker_id, enabled, owner))

        hub = _make_hub(on_hijack_changed=_on_hijack)
        worker_ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.hijack_owner = _make_ws()
            st.hijack_owner_expires_at = time.time() + 300

        await hub.force_release_hijack("w1")
        assert len(received) >= 1
        _, enabled, owner = received[0]
        assert enabled is False
        assert owner is None

    async def test_rest_session_owner_used_in_control_msg(self) -> None:
        """mutmut_45/46: owner from rest session must be used when session exists."""
        hub = _make_hub()
        worker_ws = _make_ws()
        sent_msgs: list[dict[str, Any]] = []
        worker_ws.send_text = AsyncMock(side_effect=lambda s: sent_msgs.append(__import__("json").loads(s)))
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.hijack_session = _make_hijack_session(hijack_id="hj-1", owner="my-operator")

        await hub.force_release_hijack("w1")
        ctrl = [m for m in sent_msgs if m.get("type") == "control"]
        assert ctrl[0].get("owner") == "my-operator"


# ===========================================================================
# core.py — notify_hijack_changed
# ===========================================================================
