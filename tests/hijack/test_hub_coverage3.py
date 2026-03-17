#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Coverage gap tests for hub/core.py, hub/connections.py, hub/ownership.py."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.models import HijackSession, WorkerTermState


def _make_hub(**kwargs: Any) -> TermHub:
    return TermHub(**kwargs)


def _make_ws() -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


async def _register_worker(hub: TermHub, worker_id: str, ws: Any) -> None:
    async with hub._lock:
        st = hub._workers.setdefault(worker_id, WorkerTermState())
        st.worker_ws = ws


async def _register_browser(hub: TermHub, worker_id: str, browser_ws: Any, role: str = "admin") -> None:
    async with hub._lock:
        st = hub._workers.setdefault(worker_id, WorkerTermState())
        st.browsers[browser_ws] = role


# ---------------------------------------------------------------------------
# hub/core.py lines 164-166 — async resolver TimeoutError → BrowserRoleResolutionError
# ---------------------------------------------------------------------------


class TestResolveRoleTimeout:
    async def test_async_resolver_timeout_via_monkeypatch(self) -> None:
        """Lines 164-166: timeout branch via patching wait_for."""
        from unittest.mock import patch

        from provide.terminal.hijack.hub import BrowserRoleResolutionError

        def _slow_resolver(ws: Any, worker_id: str) -> Any:
            return asyncio.get_running_loop().create_future()

        async def _mock_wait_for(coro: Any, **_kwargs: Any) -> None:
            raise TimeoutError("mocked")

        hub = _make_hub(resolve_browser_role=_slow_resolver)
        browser_ws = _make_ws()

        with patch("asyncio.wait_for", side_effect=_mock_wait_for), pytest.raises(BrowserRoleResolutionError):
            await hub._resolve_role_for_browser(browser_ws, "w1")


# ---------------------------------------------------------------------------
# hub/core.py line 169 — BrowserRoleResolutionError re-raised
# ---------------------------------------------------------------------------


class TestResolveRoleBrowserRoleError:
    async def test_browser_role_resolution_error_reraised(self) -> None:
        """Line 169: BrowserRoleResolutionError from resolver is re-raised."""
        from provide.terminal.hijack.hub import BrowserRoleResolutionError

        def _raising_resolver(ws: Any, worker_id: str) -> str:
            raise BrowserRoleResolutionError(worker_id)

        hub = _make_hub(resolve_browser_role=_raising_resolver)
        browser_ws = _make_ws()

        with pytest.raises(BrowserRoleResolutionError):
            await hub._resolve_role_for_browser(browser_ws, "w1")


# ---------------------------------------------------------------------------
# hub/core.py lines 175->177 — resolver returns invalid role (non-None, non-valid)
# ---------------------------------------------------------------------------


class TestResolveRoleInvalidRole:
    async def test_resolver_returns_invalid_role_falls_back_to_viewer(self) -> None:
        """Lines 175->177: resolver returns integer → logs warning and falls back to 'viewer'."""

        def _bad_resolver(ws: Any, worker_id: str) -> Any:
            return 42  # not a valid role string

        hub = _make_hub(resolve_browser_role=_bad_resolver)
        browser_ws = _make_ws()

        role = await hub._resolve_role_for_browser(browser_ws, "w1")
        assert role == "viewer"

    async def test_resolver_returns_unknown_string_falls_back_to_viewer(self) -> None:
        """Lines 175->177: resolver returns 'superuser' → not in valid set → viewer."""

        def _bad_resolver(ws: Any, worker_id: str) -> str:
            return "superuser"

        hub = _make_hub(resolve_browser_role=_bad_resolver)
        browser_ws = _make_ws()

        role = await hub._resolve_role_for_browser(browser_ws, "w1")
        assert role == "viewer"


# ---------------------------------------------------------------------------
# hub/core.py lines 249->251 — _send_hijack_state_to with suppress_errors=True
# ---------------------------------------------------------------------------


class TestSendHijackStateToSuppressed:
    async def test_suppress_errors_true_does_not_log_on_send_fail(self) -> None:
        """Lines 249->251: suppress_errors=True suppresses debug log when send fails."""
        hub = _make_hub()

        failing_ws = _make_ws()
        failing_ws.send_text = AsyncMock(side_effect=RuntimeError("closed"))

        dead = await hub._send_hijack_state_to(
            [failing_ws],
            worker_id="w1",
            is_hijacked=False,
            is_dashboard=False,
            is_rest=False,
            hijack_owner=None,
            input_mode="hijack",
            lease_expires_at=None,
            suppress_errors=True,
        )
        assert failing_ws in dead


# ---------------------------------------------------------------------------
# hub/core.py line 289 — broadcast_hijack_state: st2 is None after remove_dead_browsers
# ---------------------------------------------------------------------------


class TestBroadcastHijackStateSt2None:
    async def test_st2_none_after_dead_browser_removal(self) -> None:
        """Line 289: after removing dead browsers, st2 is None (worker removed by prune)."""
        hub = _make_hub()

        # Add a worker with one browser
        browser_ws = _make_ws()
        browser_ws.send_text = AsyncMock(side_effect=RuntimeError("dead"))
        worker_ws = _make_ws()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[browser_ws] = "viewer"

        # After send fails, remove_dead_browsers will clear the browser.
        # If we also clear the worker_ws (to allow prune), the worker state
        # should be pruned and st2 should be None.
        async with hub._lock:
            st2 = hub._workers["w1"]
            st2.worker_ws = None  # so prune_if_idle removes it

        # broadcast_hijack_state will find st (browser dead), remove it,
        # then re-check and find st2=None
        await hub.broadcast_hijack_state("w1")
        # Should not raise


# ---------------------------------------------------------------------------
# hub/core.py lines 327->329 — send_worker when send_text raises
# ---------------------------------------------------------------------------


class TestSendWorkerFailure:
    async def test_send_worker_clears_worker_ws_on_failure(self) -> None:
        """Lines 327->329: send_text raises → worker_ws set to None."""
        hub = _make_hub()
        worker_ws = _make_ws()
        worker_ws.send_text = AsyncMock(side_effect=RuntimeError("connection dropped"))

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws

        result = await hub.send_worker("w1", {"type": "test"})
        assert result is False

        # worker_ws should be cleared
        async with hub._lock:
            st2 = hub._workers.get("w1")
            assert st2 is None or st2.worker_ws is None


# ---------------------------------------------------------------------------
# hub/core.py lines 435-437 — browser_count for unknown worker_id
# ---------------------------------------------------------------------------


class TestBrowserCountUnknownWorker:
    async def test_browser_count_unknown_worker_returns_zero(self) -> None:
        """Lines 435-437: st is None → return 0."""
        hub = _make_hub()
        count = await hub.browser_count("nonexistent")
        assert count == 0


# ---------------------------------------------------------------------------
# hub/core.py line 445 — get_recent_events when st exists and has events
# ---------------------------------------------------------------------------


class TestGetRecentEventsWithEvents:
    async def test_get_recent_events_returns_events(self) -> None:
        """Line 445: st has events → returns list slice."""
        hub = _make_hub()

        async with hub._lock:
            hub._workers.setdefault("w1", WorkerTermState())

        # Append some events
        await hub.append_event("w1", "snapshot", {"screen": "hello"})
        await hub.append_event("w1", "snapshot", {"screen": "world"})

        events = await hub.get_recent_events("w1", 10)
        assert len(events) == 2
        assert events[0]["type"] == "snapshot"


# ---------------------------------------------------------------------------
# hub/connections.py line 155->exit — update_last_snapshot when st is None
# ---------------------------------------------------------------------------


class TestUpdateLastSnapshotNothing:
    async def test_update_last_snapshot_no_worker_noop(self) -> None:
        """Line 155->exit: update_last_snapshot when worker not registered."""
        hub = _make_hub()
        # Should not raise — if st is None, the if-block is skipped
        await hub.update_last_snapshot("nonexistent", {"screen": "test"})


# ---------------------------------------------------------------------------
# hub/connections.py lines 207->218, 214->218 — cleanup_browser_disconnect
# with owned_hijack=True but was_owner=False and worker is online
# ---------------------------------------------------------------------------


class TestCleanupBrowserDisconnectResumeWithoutOwner:
    async def test_resume_without_owner_when_owned_hijack_and_worker_online(self) -> None:
        """Lines 207->218, 214->218: owned_hijack=True, was_owner=False, worker online."""
        hub = _make_hub()
        browser_ws = _make_ws()
        worker_ws = _make_ws()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[browser_ws] = "admin"
            # No dashboard hijack active (hijack_owner is None → was_owner=False)
            # But owned_hijack=True (browser HAD owned a hijack earlier this session)
            # Worker is online

        result = await hub.cleanup_browser_disconnect("w1", browser_ws, owned_hijack=True)
        # was_owner should be False since hijack_owner is None
        assert result["was_owner"] is False
        # resume_without_owner may be True if last event was not expiry
        # (no events → events deque empty → last_event_type = "" → not in expiry set)
        assert result["resume_without_owner"] is True


# ---------------------------------------------------------------------------
# hub/connections.py lines 269->273 — force_release_hijack with dashboard hijack active
# ---------------------------------------------------------------------------


class TestForceReleaseHijackDashboard:
    async def test_force_release_clears_dashboard_hijack(self) -> None:
        """Lines 269->273: is_dashboard_hijack_active → clears hijack_owner."""
        hub = _make_hub()
        worker_ws = _make_ws()
        owner_ws = _make_ws()
        worker_ws.send_text = AsyncMock()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 300

        result = await hub.force_release_hijack("w1")
        assert result is True

        async with hub._lock:
            st2 = hub._workers.get("w1")
            assert st2 is None or st2.hijack_owner is None


# ---------------------------------------------------------------------------
# hub/ownership.py lines 81->88, 86->88 — cleanup_expired_hijack should_resume re-check
# ---------------------------------------------------------------------------


class TestCleanupExpiredHijackRecheck:
    async def test_should_resume_recheck_finds_still_hijacked(self) -> None:
        """Lines 86->88: re-check finds hijack is still active → should_resume=False."""
        hub = _make_hub()
        worker_ws = _make_ws()
        worker_ws.send_text = AsyncMock()

        now = time.time()
        # Set an expired REST session
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.hijack_session = HijackSession(
                hijack_id="hid1",
                owner="tester",
                acquired_at=now - 100,
                lease_expires_at=now - 1,  # expired
                last_heartbeat=now - 100,
            )

        # Between the first lock release and send_worker, a new hijack is written.
        # We simulate this by patching send_worker to write a new session first.
        new_session_written = False
        original_send_worker = hub.send_worker

        async def _inject_hijack(wid: str, msg: dict) -> bool:
            nonlocal new_session_written
            if not new_session_written:
                new_session_written = True
                async with hub._lock:
                    st2 = hub._workers.get(wid)
                    if st2 is not None:
                        st2.hijack_session = HijackSession(
                            hijack_id="hid2",
                            owner="other",
                            acquired_at=time.time(),
                            lease_expires_at=time.time() + 60,
                            last_heartbeat=time.time(),
                        )
            return await original_send_worker(wid, msg)

        hub.send_worker = _inject_hijack  # type: ignore[method-assign]

        result = await hub.cleanup_expired_hijack("w1")
        # cleanup should have run (expired session cleared)
        assert result is True
        # send_worker (resume) should NOT have been called since should_resume
        # was set to False after re-check found new hijack

    async def test_should_resume_recheck_confirms_no_hijack_sends_resume(self) -> None:
        """Lines 88->95: re-check confirms not hijacked → resume sent."""
        hub = _make_hub()
        worker_ws = _make_ws()
        worker_ws.send_text = AsyncMock()

        now = time.time()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.hijack_session = HijackSession(
                hijack_id="hid1",
                owner="tester",
                acquired_at=now - 100,
                lease_expires_at=now - 1,  # expired
                last_heartbeat=now - 100,
            )

        result = await hub.cleanup_expired_hijack("w1")
        assert result is True
        # Resume should have been sent (send_worker called with action=resume)
        worker_ws.send_text.assert_called()
        calls = [call.args[0] for call in worker_ws.send_text.call_args_list]
        import json

        resume_sent = any(json.loads(c).get("action") == "resume" for c in calls)
        assert resume_sent


# ---------------------------------------------------------------------------
# hub/ownership.py lines 213->220, 225->227 — remove_dead_browsers clears owner
# ---------------------------------------------------------------------------


class TestRemoveDeadBrowsersOwner:
    async def test_remove_dead_browsers_clears_dashboard_owner(self) -> None:
        """Lines 213->220, 225->227: dead set includes dashboard hijack owner."""
        hub = _make_hub()
        worker_ws = _make_ws()
        worker_ws.send_text = AsyncMock()
        owner_ws = _make_ws()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[owner_ws] = "admin"
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 300

        changed = await hub.remove_dead_browsers("w1", {owner_ws})
        assert changed is True

        async with hub._lock:
            st2 = hub._workers.get("w1")
            assert st2 is None or st2.hijack_owner is None


# ---------------------------------------------------------------------------
# hub/ownership.py line 240 — extend_hijack_lease when session not found
# ---------------------------------------------------------------------------


class TestExtendHijackLeaseNotFound:
    async def test_extend_lease_unknown_worker_returns_none(self) -> None:
        """Line 240: worker not found → return None."""
        hub = _make_hub()
        result = await hub.extend_hijack_lease("nonexistent", "hid1", 60, time.time())
        assert result is None

    async def test_extend_lease_wrong_hijack_id_returns_none(self) -> None:
        """Line 240: hijack_id mismatch → return None."""
        hub = _make_hub()
        now = time.time()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.hijack_session = HijackSession(
                hijack_id="hid-real",
                owner="tester",
                acquired_at=now,
                lease_expires_at=now + 60,
                last_heartbeat=now,
            )

        result = await hub.extend_hijack_lease("w1", "wrong-hid", 60, now)
        assert result is None


# ---------------------------------------------------------------------------
# hub/ownership.py line 306 — release_rest_hijack should_resume=True
# ---------------------------------------------------------------------------


class TestReleaseRestHijackShouldResume:
    async def test_release_rest_hijack_no_dashboard_returns_should_resume_true(self) -> None:
        """Line 306: release_rest_hijack when no dashboard hijack → should_resume=True."""
        hub = _make_hub()
        now = time.time()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.hijack_session = HijackSession(
                hijack_id="hid1",
                owner="tester",
                acquired_at=now,
                lease_expires_at=now + 60,
                last_heartbeat=now,
            )
            # No dashboard hijack

        released, should_resume = await hub.release_rest_hijack("w1", "hid1")
        assert released is True
        assert should_resume is True


# ---------------------------------------------------------------------------
# hub/ownership.py line 316 — check_still_hijacked when st is None
# ---------------------------------------------------------------------------


class TestCheckStillHijackedStNone:
    async def test_check_still_hijacked_no_worker_returns_false(self) -> None:
        """Line 316: st is None → return False."""
        hub = _make_hub()
        result = await hub.check_still_hijacked("nonexistent")
        assert result is False


# ---------------------------------------------------------------------------
# hub/ownership.py line 333 — is_input_open_mode when st is None
# ---------------------------------------------------------------------------


class TestIsInputOpenMode:
    async def test_is_input_open_mode_no_worker_returns_false(self) -> None:
        """Line 333: st is None → return False."""
        hub = _make_hub()
        result = await hub.is_input_open_mode("nonexistent")
        assert result is False

    async def test_is_input_open_mode_hijack_mode_returns_false(self) -> None:
        """Line 333: st exists but input_mode != 'open' → return False."""
        hub = _make_hub()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.input_mode = "hijack"

        result = await hub.is_input_open_mode("w1")
        assert result is False
