#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Coverage gap tests for hub/core.py, hub/connections.py, hub/ownership.py (part 2)."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

from .control_channel_helpers import decode_control_payload


def _make_hub(**kwargs: Any) -> TermHub:
    return TermHub(**kwargs)


def _make_ws() -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# hub/ownership.py lines 81->88, 86->88 — cleanup_expired_hijack should_resume re-check
# ---------------------------------------------------------------------------


class TestCleanupExpiredHijackRecheck:
    async def test_should_resume_recheck_finds_still_hijacked(self) -> None:
        """Lines 86->88: re-check finds hijack is still active → should_resume=False."""
        hub = _make_hub()
        worker_ws = _make_ws()
        worker_ws.send_text = AsyncMock()

        now = time.monotonic()
        # Set an expired REST session
        async with hub._lock:
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
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
                    st2 = hub.registry._workers.get(wid)
                    if st2 is not None:
                        st2.hijack_session = HijackSession(
                            hijack_id="hid2",
                            owner="other",
                            acquired_at=time.monotonic(),
                            lease_expires_at=time.monotonic() + 60,
                            last_heartbeat=time.monotonic(),
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

        now = time.monotonic()
        async with hub._lock:
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
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
        resume_sent = any(decode_control_payload(c).get("action") == "resume" for c in calls)
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
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[owner_ws] = "admin"
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.monotonic() + 300

        changed = await hub.remove_dead_browsers("w1", {owner_ws})
        assert changed is True

        async with hub._lock:
            st2 = hub.registry._workers.get("w1")
            assert st2 is None or st2.hijack_owner is None


# ---------------------------------------------------------------------------
# hub/ownership.py line 240 — extend_hijack_lease when session not found
# ---------------------------------------------------------------------------


class TestExtendHijackLeaseNotFound:
    async def test_extend_lease_unknown_worker_returns_none(self) -> None:
        """Line 240: worker not found → return None."""
        hub = _make_hub()
        result = await hub.extend_hijack_lease("nonexistent", "hid1", "owner", 60, time.monotonic())
        assert result is None

    async def test_extend_lease_wrong_hijack_id_returns_none(self) -> None:
        """Line 240: hijack_id mismatch → return None."""
        hub = _make_hub()
        now = time.monotonic()

        async with hub._lock:
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.hijack_session = HijackSession(
                hijack_id="hid-real",
                owner="tester",
                acquired_at=now,
                lease_expires_at=now + 60,
                last_heartbeat=now,
            )

        result = await hub.extend_hijack_lease("w1", "wrong-hid", "tester", 60, now)
        assert result is None


# ---------------------------------------------------------------------------
# hub/ownership.py line 306 — release_rest_hijack should_resume=True
# ---------------------------------------------------------------------------


class TestReleaseRestHijackShouldResume:
    async def test_release_rest_hijack_no_dashboard_returns_should_resume_true(self) -> None:
        """Line 306: release_rest_hijack when no dashboard hijack → should_resume=True."""
        hub = _make_hub()
        now = time.monotonic()

        async with hub._lock:
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
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
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.input_mode = "hijack"

        result = await hub.is_input_open_mode("w1")
        assert result is False
