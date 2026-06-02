#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ConnectionManager browser-disconnect surface.

Targets three methods on
``provide.uterm.server.bridge.hub.connection.ConnectionManager``:

* ``cleanup_browser_disconnect`` — the public WS-disconnect handler.
* ``_update_lock_state`` — the under-lock state-mutation helper.
* ``_scan_events_for_resume`` — the event-history backwards scan.

Every test constructs a FRESH ``TermHub()`` and pins every observable:
exact return dict/tuple, every ``WorkerTermState`` field mutation, every
hub-map mutation (``_principal_browser_counts`` / ``_ws_principal`` /
``_ws_to_resume_token`` / ``_startup_pending_browsers``), the resume-store
``mark_hijack_owner`` delegation, the ``on_worker_empty`` callback, and the
exact tracer span + ``EVENT_SESSION_DISCONNECTED`` log call.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.hub.ext import EVENT_SESSION_DISCONNECTED
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ws() -> MagicMock:
    """A browser/worker WebSocket double with the async surface the hub touches."""
    ws = MagicMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    # No real principal attached: _browser_principal_subject_id returns None.
    ws.state = MagicMock()
    ws.state.uterm_principal = None
    return ws


def _rest_session(*, expires_in: float) -> HijackSession:
    now = time.monotonic()
    return HijackSession(
        hijack_id="rest-hid",
        owner="rest-owner",
        acquired_at=now,
        lease_expires_at=now + expires_in,
        last_heartbeat=now,
    )


async def _put(hub: TermHub, worker_id: str, st: WorkerTermState) -> None:
    async with hub._lock:
        hub._workers[worker_id] = st


# ===========================================================================
# _scan_events_for_resume — pure static scan over st.events
# ===========================================================================


class TestScanEventsForResume:
    """``_scan_events_for_resume`` returns False iff an expiry event precedes
    (scanning backwards) any acquire/release stop event; else True."""

    def _scan(self, events: list[dict[str, Any]]) -> bool:
        hub = TermHub()
        st = WorkerTermState()
        st.events.extend(events)
        return hub.connection_mgr._scan_events_for_resume(st)

    def test_empty_history_returns_true(self) -> None:
        """No events at all -> nothing already sent a resume -> True."""
        assert self._scan([]) is True

    def test_unrelated_events_only_returns_true(self) -> None:
        """Only non-lifecycle events -> scan runs to exhaustion -> True."""
        assert self._scan([{"type": "snapshot"}, {"type": "term"}]) is True

    def test_owner_expired_returns_false(self) -> None:
        """A hijack_owner_expired event (nothing newer stops first) -> False."""
        assert self._scan([{"type": "hijack_owner_expired"}]) is False

    def test_lease_expired_returns_false(self) -> None:
        """A hijack_lease_expired event -> False."""
        assert self._scan([{"type": "hijack_lease_expired"}]) is False

    def test_expiry_after_snapshot_still_false_scanning_backwards(self) -> None:
        """A snapshot AFTER the expiry must not mask it (backwards scan reaches
        the expiry before any stop event) -> False."""
        assert self._scan([{"type": "hijack_owner_expired"}, {"type": "snapshot"}]) is False

    def test_acquired_event_stops_scan_returns_true(self) -> None:
        """A hijack_acquired event encountered first breaks the loop -> True."""
        assert self._scan([{"type": "hijack_acquired"}]) is True

    def test_released_event_stops_scan_returns_true(self) -> None:
        """A hijack_released event encountered first breaks the loop -> True."""
        assert self._scan([{"type": "hijack_released"}]) is True

    def test_acquire_shields_earlier_expiry_returns_true(self) -> None:
        """Backwards scan hits the newer acquire (break) before the older
        expiry, so the expiry is never seen -> True. Kills a forward-scan or
        no-break mutation."""
        # Order: oldest ... newest. Newest (scanned first) is the acquire.
        assert self._scan([{"type": "hijack_owner_expired"}, {"type": "hijack_acquired"}]) is True

    def test_expiry_newer_than_acquire_returns_false(self) -> None:
        """When the expiry is the newest event, it is seen before the older
        acquire stop -> False."""
        assert self._scan([{"type": "hijack_acquired"}, {"type": "hijack_owner_expired"}]) is False

    def test_missing_type_key_defaults_empty_string(self) -> None:
        """Events with no 'type' key coerce to '' (not a lifecycle event) -> True."""
        assert self._scan([{"foo": "bar"}]) is True


# ===========================================================================
# _update_lock_state — under-lock mutations + 3-tuple outcome
# ===========================================================================


class TestUpdateLockState:
    """``_update_lock_state`` returns ``(was_owner, rest_still_active,
    resume_without_owner)`` and mutates st.browsers / hub counts / owner."""

    def test_pops_ws_from_browsers(self) -> None:
        hub = TermHub()
        st = WorkerTermState()
        ws = _ws()
        other = _ws()
        st.browsers[ws] = "viewer"
        st.browsers[other] = "admin"
        hub.connection_mgr._update_lock_state(st, ws, False)
        assert ws not in st.browsers
        assert st.browsers == {other: "admin"}

    def test_not_owner_not_owned_returns_all_false(self) -> None:
        """Plain viewer disconnect, no hijack involvement -> (False, False, False)."""
        hub = TermHub()
        st = WorkerTermState()
        ws = _ws()
        st.browsers[ws] = "viewer"
        result = hub.connection_mgr._update_lock_state(st, ws, False)
        assert result == (False, False, False)

    def test_decrements_principal_count_to_remaining(self) -> None:
        """Counted ws with count 2 -> decrement to 1, key retained, _ws_principal popped."""
        hub = TermHub()
        st = WorkerTermState()
        ws = _ws()
        st.browsers[ws] = "viewer"
        hub._ws_principal[ws] = "alice"
        hub._principal_browser_counts["alice"] = 2
        hub.connection_mgr._update_lock_state(st, ws, False)
        assert hub._principal_browser_counts["alice"] == 1
        assert ws not in hub._ws_principal

    def test_decrements_principal_count_pops_at_zero(self) -> None:
        """Count 1 -> remaining 0 -> the subject key is popped entirely."""
        hub = TermHub()
        st = WorkerTermState()
        ws = _ws()
        st.browsers[ws] = "viewer"
        hub._ws_principal[ws] = "bob"
        hub._principal_browser_counts["bob"] = 1
        hub.connection_mgr._update_lock_state(st, ws, False)
        assert "bob" not in hub._principal_browser_counts
        assert ws not in hub._ws_principal

    def test_uncounted_ws_leaves_counts_untouched(self) -> None:
        """ws not in _ws_principal -> no decrement attempted."""
        hub = TermHub()
        st = WorkerTermState()
        ws = _ws()
        st.browsers[ws] = "viewer"
        hub._principal_browser_counts["carol"] = 3
        hub.connection_mgr._update_lock_state(st, ws, False)
        assert hub._principal_browser_counts == {"carol": 3}

    def test_was_owner_clears_owner_and_no_rest(self) -> None:
        """Active dashboard owner is this ws -> was_owner True, owner fields
        cleared, rest_still_active False (no REST session)."""
        hub = TermHub()
        st = WorkerTermState()
        ws = _ws()
        st.worker_ws = _ws()
        st.browsers[ws] = "admin"
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 60
        result = hub.connection_mgr._update_lock_state(st, ws, False)
        assert result == (True, False, False)
        assert st.hijack_owner is None
        assert st.hijack_owner_expires_at is None

    def test_was_owner_with_rest_lease_reports_rest_still_active(self) -> None:
        """Owner disconnect while a valid REST lease remains -> rest_still_active True."""
        hub = TermHub()
        st = WorkerTermState()
        ws = _ws()
        st.worker_ws = _ws()
        st.browsers[ws] = "admin"
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 60
        st.hijack_session = _rest_session(expires_in=60)
        result = hub.connection_mgr._update_lock_state(st, ws, False)
        assert result == (True, True, False)
        # was_owner branch never touches resume_without_owner.
        assert result[2] is False

    def test_was_owner_expired_rest_reports_rest_inactive(self) -> None:
        """Owner disconnect with a stale REST lease -> rest_still_active False."""
        hub = TermHub()
        st = WorkerTermState()
        ws = _ws()
        st.worker_ws = _ws()
        st.browsers[ws] = "admin"
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 60
        st.hijack_session = _rest_session(expires_in=-5)
        result = hub.connection_mgr._update_lock_state(st, ws, False)
        assert result == (True, False, False)

    def test_owner_is_other_ws_is_not_was_owner(self) -> None:
        """Dashboard hijack active but owner is a DIFFERENT ws -> was_owner False;
        owner fields left intact."""
        hub = TermHub()
        st = WorkerTermState()
        owner_ws = _ws()
        ws = _ws()
        st.worker_ws = _ws()
        st.browsers[ws] = "viewer"
        st.browsers[owner_ws] = "admin"
        st.hijack_owner = owner_ws
        st.hijack_owner_expires_at = time.monotonic() + 60
        # owned_hijack False so the elif resume branch does not run.
        result = hub.connection_mgr._update_lock_state(st, ws, False)
        assert result == (False, False, False)
        assert st.hijack_owner is owner_ws

    def test_owned_hijack_branch_computes_resume_true(self) -> None:
        """elif owned_hijack branch: worker online, not hijacked, clean event
        history -> resume_without_owner True via _scan_events_for_resume."""
        hub = TermHub()
        st = WorkerTermState()
        ws = _ws()
        st.worker_ws = _ws()
        st.browsers[ws] = "admin"
        # No active hijack: hijack_owner None, no session -> is_hijacked False.
        st.events.append({"type": "snapshot"})
        result = hub.connection_mgr._update_lock_state(st, ws, owned_hijack=True)
        assert result == (False, False, True)

    def test_owned_hijack_branch_computes_resume_false_on_expiry(self) -> None:
        """elif owned_hijack branch with an expiry event -> resume_without_owner False."""
        hub = TermHub()
        st = WorkerTermState()
        ws = _ws()
        st.worker_ws = _ws()
        st.browsers[ws] = "admin"
        st.events.append({"type": "hijack_lease_expired"})
        result = hub.connection_mgr._update_lock_state(st, ws, owned_hijack=True)
        assert result == (False, False, False)

    def test_owned_hijack_but_worker_offline_no_resume(self) -> None:
        """owned_hijack True but worker_ws is None -> elif guard fails ->
        resume_without_owner stays False (no scan)."""
        hub = TermHub()
        st = WorkerTermState()
        ws = _ws()
        st.worker_ws = None
        st.browsers[ws] = "admin"
        st.events.append({"type": "snapshot"})
        result = hub.connection_mgr._update_lock_state(st, ws, owned_hijack=True)
        assert result == (False, False, False)

    def test_owned_hijack_but_still_hijacked_no_resume(self) -> None:
        """owned_hijack True but a valid REST lease keeps is_hijacked True ->
        elif guard fails -> resume_without_owner False."""
        hub = TermHub()
        st = WorkerTermState()
        ws = _ws()
        st.worker_ws = _ws()
        st.browsers[ws] = "admin"
        # Active REST lease, but ws is not the dashboard owner, so was_owner is
        # False and we reach the elif; is_hijacked() is True -> guard fails.
        st.hijack_session = _rest_session(expires_in=60)
        st.events.append({"type": "snapshot"})
        result = hub.connection_mgr._update_lock_state(st, ws, owned_hijack=True)
        assert result == (False, False, False)

    def test_was_owner_short_circuits_owned_hijack_branch(self) -> None:
        """When was_owner is True the elif is never evaluated even if owned_hijack
        is True -> resume_without_owner stays False."""
        hub = TermHub()
        st = WorkerTermState()
        ws = _ws()
        st.worker_ws = _ws()
        st.browsers[ws] = "admin"
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 60
        st.events.append({"type": "snapshot"})  # would yield resume True if scanned
        result = hub.connection_mgr._update_lock_state(st, ws, owned_hijack=True)
        assert result == (True, False, False)


# ===========================================================================
# cleanup_browser_disconnect — public handler
# ===========================================================================


class TestCleanupBrowserDisconnect:
    """``cleanup_browser_disconnect`` returns the exact 3-key dict, marks the
    resume token, fires on_worker_empty, and emits the disconnect span+log."""

    async def test_unknown_worker_returns_all_false(self) -> None:
        """st is None -> every flag False (browser_count stays -1, no callback)."""
        hub = TermHub()
        result = await hub.cleanup_browser_disconnect("nope", _ws(), False)
        assert result == {
            "was_owner": False,
            "rest_still_active": False,
            "resume_without_owner": False,
        }

    async def test_was_owner_with_rest_active_dict(self) -> None:
        """Owner disconnect + valid REST lease -> was_owner & rest_still_active True,
        resume_without_owner False; owner fields cleared on st."""
        hub = TermHub()
        ws = _ws()
        other = _ws()
        st = WorkerTermState()
        st.worker_ws = _ws()
        st.browsers[ws] = "admin"
        st.browsers[other] = "viewer"
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 60
        st.hijack_session = _rest_session(expires_in=60)
        await _put(hub, "w1", st)
        result = await hub.cleanup_browser_disconnect("w1", ws, False)
        assert result == {
            "was_owner": True,
            "rest_still_active": True,
            "resume_without_owner": False,
        }
        assert ws not in st.browsers
        assert st.hijack_owner is None
        assert st.hijack_owner_expires_at is None

    async def test_resume_without_owner_dict(self) -> None:
        """owned_hijack, worker online, not hijacked, clean events -> resume True."""
        hub = TermHub()
        ws = _ws()
        st = WorkerTermState()
        st.worker_ws = _ws()
        st.browsers[ws] = "admin"
        st.events.append({"type": "snapshot"})
        await _put(hub, "w1", st)
        result = await hub.cleanup_browser_disconnect("w1", ws, owned_hijack=True)
        assert result == {
            "was_owner": False,
            "rest_still_active": False,
            "resume_without_owner": True,
        }

    async def test_resume_without_owner_false_on_expiry_event(self) -> None:
        """owned_hijack but a hijack_owner_expired event already fired a resume."""
        hub = TermHub()
        ws = _ws()
        st = WorkerTermState()
        st.worker_ws = _ws()
        st.browsers[ws] = "admin"
        st.events.append({"type": "hijack_owner_expired"})
        await _put(hub, "w1", st)
        result = await hub.cleanup_browser_disconnect("w1", ws, owned_hijack=True)
        assert result["resume_without_owner"] is False

    # -- resume-store mark_hijack_owner delegation -------------------------

    async def test_marks_resume_token_when_was_owner(self) -> None:
        """Token present + was_owner -> mark_hijack_owner(token, True); token popped."""
        hub = TermHub()
        store = MagicMock()
        store.mark_hijack_owner = AsyncMock()
        hub._resume_store = store
        ws = _ws()
        st = WorkerTermState()
        st.worker_ws = _ws()
        st.browsers[ws] = "admin"
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 60
        await _put(hub, "w1", st)
        hub._ws_to_resume_token[ws] = "tok-1"
        await hub.cleanup_browser_disconnect("w1", ws, False)
        store.mark_hijack_owner.assert_awaited_once_with("tok-1", True)
        assert ws not in hub._ws_to_resume_token

    async def test_marks_resume_token_when_owned_hijack_flag(self) -> None:
        """Token present + owned_hijack True (but not was_owner) -> still marks."""
        hub = TermHub()
        store = MagicMock()
        store.mark_hijack_owner = AsyncMock()
        hub._resume_store = store
        ws = _ws()
        st = WorkerTermState()
        st.worker_ws = _ws()
        st.browsers[ws] = "admin"
        st.events.append({"type": "snapshot"})
        await _put(hub, "w1", st)
        hub._ws_to_resume_token[ws] = "tok-2"
        await hub.cleanup_browser_disconnect("w1", ws, owned_hijack=True)
        store.mark_hijack_owner.assert_awaited_once_with("tok-2", True)

    async def test_does_not_mark_when_neither_owner_nor_owned(self) -> None:
        """Token present but was_owner False and owned_hijack False -> no mark;
        token is still popped from the map."""
        hub = TermHub()
        store = MagicMock()
        store.mark_hijack_owner = AsyncMock()
        hub._resume_store = store
        ws = _ws()
        st = WorkerTermState()
        st.worker_ws = _ws()
        st.browsers[ws] = "viewer"
        await _put(hub, "w1", st)
        hub._ws_to_resume_token[ws] = "tok-3"
        await hub.cleanup_browser_disconnect("w1", ws, False)
        store.mark_hijack_owner.assert_not_awaited()
        assert ws not in hub._ws_to_resume_token

    async def test_no_mark_when_token_absent(self) -> None:
        """Resume store set but no token for ws -> mark not called even if owner."""
        hub = TermHub()
        store = MagicMock()
        store.mark_hijack_owner = AsyncMock()
        hub._resume_store = store
        ws = _ws()
        st = WorkerTermState()
        st.worker_ws = _ws()
        st.browsers[ws] = "admin"
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 60
        await _put(hub, "w1", st)
        # no _ws_to_resume_token entry
        await hub.cleanup_browser_disconnect("w1", ws, False)
        store.mark_hijack_owner.assert_not_awaited()

    async def test_no_resume_store_skips_token_pop(self) -> None:
        """_resume_store None -> token-pop block skipped, token left in the map."""
        hub = TermHub()
        hub._resume_store = None
        ws = _ws()
        st = WorkerTermState()
        st.worker_ws = _ws()
        st.browsers[ws] = "admin"
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 60
        await _put(hub, "w1", st)
        hub._ws_to_resume_token[ws] = "tok-keep"
        await hub.cleanup_browser_disconnect("w1", ws, False)
        # The _resume_store is None branch never pops the token map.
        assert hub._ws_to_resume_token.get(ws) == "tok-keep"

    # -- startup-pending discard -------------------------------------------

    async def test_discards_from_startup_pending(self) -> None:
        """ws is always removed from _startup_pending_browsers on disconnect."""
        hub = TermHub()
        ws = _ws()
        st = WorkerTermState()
        st.worker_ws = _ws()
        st.browsers[ws] = "viewer"
        await _put(hub, "w1", st)
        hub._startup_pending_browsers.add(ws)
        await hub.cleanup_browser_disconnect("w1", ws, False)
        assert ws not in hub._startup_pending_browsers

    # -- on_worker_empty callback ------------------------------------------

    async def test_fires_on_worker_empty_when_last_browser(self) -> None:
        """browser_count == 0 after pop + callback set -> on_worker_empty(worker_id)."""
        hub = TermHub()
        seen: list[str] = []

        async def on_empty(wid: str) -> None:
            seen.append(wid)

        hub.on_worker_empty = on_empty
        ws = _ws()
        st = WorkerTermState()
        st.browsers[ws] = "viewer"
        await _put(hub, "w1", st)
        await hub.cleanup_browser_disconnect("w1", ws, False)
        await asyncio.sleep(0.02)
        assert seen == ["w1"]

    async def test_no_callback_when_other_browsers_remain(self) -> None:
        """browser_count > 0 (a second browser stays) -> callback NOT fired."""
        hub = TermHub()
        seen: list[str] = []

        async def on_empty(wid: str) -> None:
            seen.append(wid)

        hub.on_worker_empty = on_empty
        ws = _ws()
        other = _ws()
        st = WorkerTermState()
        st.browsers[ws] = "viewer"
        st.browsers[other] = "viewer"
        await _put(hub, "w1", st)
        await hub.cleanup_browser_disconnect("w1", ws, False)
        await asyncio.sleep(0.02)
        assert seen == []

    async def test_no_callback_when_unknown_worker(self) -> None:
        """Unknown worker keeps browser_count at -1 (not 0) -> callback NOT fired."""
        hub = TermHub()
        seen: list[str] = []

        async def on_empty(wid: str) -> None:
            seen.append(wid)

        hub.on_worker_empty = on_empty
        await hub.cleanup_browser_disconnect("ghost", _ws(), False)
        await asyncio.sleep(0.02)
        assert seen == []

    async def test_no_callback_when_callback_is_none(self) -> None:
        """Last browser leaves but on_worker_empty is None -> no task scheduled."""
        hub = TermHub()
        hub.on_worker_empty = None
        ws = _ws()
        st = WorkerTermState()
        st.browsers[ws] = "viewer"
        await _put(hub, "w1", st)
        before = len(hub._background_tasks)
        await hub.cleanup_browser_disconnect("w1", ws, False)
        assert len(hub._background_tasks) == before

    async def test_callback_task_tracked_in_background_tasks(self) -> None:
        """The scheduled callback task is registered in _background_tasks."""
        hub = TermHub()
        started = asyncio.Event()
        release = asyncio.Event()

        async def on_empty(_wid: str) -> None:
            started.set()
            await release.wait()

        hub.on_worker_empty = on_empty
        ws = _ws()
        st = WorkerTermState()
        st.browsers[ws] = "viewer"
        await _put(hub, "w1", st)
        await hub.cleanup_browser_disconnect("w1", ws, False)
        await asyncio.wait_for(started.wait(), 1.0)
        assert len(hub._background_tasks) == 1
        release.set()
        await asyncio.sleep(0.02)

    # -- observability: span + log -----------------------------------------

    async def test_emits_exact_span_and_disconnect_log(self) -> None:
        """Pins the span name + attributes dict and the EVENT_SESSION_DISCONNECTED
        log with exact kwargs. Kills span-name/attr-key, event-const, and
        kwarg-drop mutations."""
        hub = TermHub()
        ws = _ws()
        st = WorkerTermState()
        st.worker_ws = _ws()
        st.browsers[ws] = "viewer"
        await _put(hub, "w1", st)
        with (
            patch("provide.uterm.server.bridge.hub.connection.logger") as mlog,
            patch("provide.uterm.server.bridge.hub.connection.tracer") as mtr,
        ):
            await hub.cleanup_browser_disconnect("w1", ws, False)
        mtr.start_as_current_span.assert_called_once_with("uterm.browser.deregister", attributes={"worker_id": "w1"})
        mlog.info.assert_called_once_with(EVENT_SESSION_DISCONNECTED, worker_id="w1", session_type="browser")

    async def test_log_uses_worker_id_argument(self) -> None:
        """The disconnect log carries the SPECIFIC worker_id, not a constant.

        Kills a mutation that hardcodes/swaps the worker_id positional/kwarg."""
        hub = TermHub()
        ws = _ws()
        st = WorkerTermState()
        st.browsers[ws] = "viewer"
        await _put(hub, "w-distinct", st)
        with (
            patch("provide.uterm.server.bridge.hub.connection.logger") as mlog,
            patch("provide.uterm.server.bridge.hub.connection.tracer"),
        ):
            await hub.cleanup_browser_disconnect("w-distinct", ws, False)
        assert mlog.info.call_args == call(EVENT_SESSION_DISCONNECTED, worker_id="w-distinct", session_type="browser")
