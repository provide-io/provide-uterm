#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Second-pass coverage gap tests — ownership expiry / dead-browser cleanup."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.models import HijackSession, WorkerTermState


def _make_ws() -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# hijack/hub/ownership.py line 81->88 — should_resume=False (dashboard expired, REST active)
# ---------------------------------------------------------------------------


class TestCleanupExpiredHijackShouldResumeFalse:
    async def test_should_resume_false_when_rest_still_active(self) -> None:
        """Line 81->88: should_resume=False (dashboard expired but REST still active)."""
        hub = TermHub()
        now = time.time()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            # Dashboard hijack expired
            fake_browser_ws = _make_ws()
            st.hijack_owner = fake_browser_ws
            st.hijack_owner_expires_at = now - 1  # expired
            # REST hijack still active
            st.hijack_session = HijackSession(
                hijack_id="rest-hid",
                owner="rest-op",
                acquired_at=now,
                lease_expires_at=now + 300,  # NOT expired
                last_heartbeat=now,
            )

        result = await hub.cleanup_expired_hijack("w1")
        assert result is True  # dashboard expired

        async with hub._lock:
            st2 = hub._workers.get("w1")
            assert st2 is not None
            assert st2.hijack_owner is None  # dashboard owner cleared
            assert st2.hijack_session is not None  # REST still active


# ---------------------------------------------------------------------------
# hijack/hub/ownership.py line 81->88, 86->88 — cleanup_expired_hijack should_resume recheck
# ---------------------------------------------------------------------------


class TestCleanupExpiredHijackShouldResumeFalseAfterRecheck:
    async def test_should_resume_blocked_by_recheck(self) -> None:
        """Lines 81->88: should_resume=True but recheck shows new hijack → skip resume."""
        hub = TermHub()
        worker_ws = _make_ws()
        now = time.time()

        # Set up an expired dashboard hijack (so cleanup will fire)
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.hijack_owner = _make_ws()  # any ws
            st.hijack_owner_expires_at = now - 1  # expired

        original_acquire = hub._lock.acquire
        acquire_count = 0

        async def _counting_acquire() -> bool:
            nonlocal acquire_count
            acquire_count += 1
            result = await original_acquire()
            # On the 2nd lock acquisition (the recheck), install a new hijack
            if acquire_count == 2:
                st = hub._workers.get("w1")
                if st is not None and st.hijack_session is None:
                    st.hijack_session = HijackSession(
                        hijack_id="new-hijack",
                        owner="concurrent-owner",
                        acquired_at=now,
                        lease_expires_at=now + 300,
                        last_heartbeat=now,
                    )
            return result  # type: ignore[return-value]

        hub._lock.acquire = _counting_acquire  # type: ignore[method-assign]

        result = await hub.cleanup_expired_hijack("w1")
        assert result is True
        # should_resume was flipped to False by recheck, so no resume was sent
        # (just verify no crash)


# ---------------------------------------------------------------------------
# hijack/hub/ownership.py line 86->88 — st2 is not None and is_hijacked (recheck True)
# ---------------------------------------------------------------------------


class TestCleanupExpiredHijackRecheckTrue:
    async def test_should_resume_false_when_recheck_finds_hijack(self) -> None:
        """Line 86->88: recheck finds is_hijacked → should_resume becomes False."""
        hub = TermHub()
        now = time.time()

        # Expired REST session — should_resume will be True after first lock
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_session = HijackSession(
                hijack_id="old-hijack",
                owner="operator",
                acquired_at=now - 100,
                lease_expires_at=now - 1,  # expired
                last_heartbeat=now - 50,
            )

        resume_calls: list = []
        original_send = hub.send_worker

        async def _capture_send(worker_id: str, msg: dict) -> bool:
            if msg.get("action") == "resume":
                resume_calls.append(msg)
            return await original_send(worker_id, msg)

        hub.send_worker = _capture_send  # type: ignore[method-assign]

        original_acquire = hub._lock.acquire
        acquire_count = 0

        async def _inject_on_second_acquire() -> bool:
            nonlocal acquire_count
            acquire_count += 1
            result = await original_acquire()
            if acquire_count == 2:
                st = hub._workers.get("w1")
                if st is not None:
                    # Make hub think something is hijacked
                    fake_ws = _make_ws()
                    st.hijack_owner = fake_ws
                    st.hijack_owner_expires_at = now + 999
            return result  # type: ignore[return-value]

        hub._lock.acquire = _inject_on_second_acquire  # type: ignore[method-assign]

        result = await hub.cleanup_expired_hijack("w1")
        assert result is True
        # Resume was NOT sent because recheck found active hijack
        assert len(resume_calls) == 0


# ---------------------------------------------------------------------------
# hijack/hub/ownership.py line 213->220 — remove_dead_browsers: st is None (False branch)
# ---------------------------------------------------------------------------


class TestRemoveDeadBrowsersStNone:
    async def test_remove_dead_browsers_nonexistent_worker(self) -> None:
        """Line 213->220: st is None (worker not found) → skip inner block."""
        hub = TermHub()
        dead_ws = _make_ws()
        # Call with a worker_id that doesn't exist
        result = await hub.remove_dead_browsers("nonexistent-worker", {dead_ws})
        assert result is False  # notify_hijack_off was never set True


# ---------------------------------------------------------------------------
# hijack/hub/ownership.py line 213->220 — remove_dead_browsers clears dashboard owner
# ---------------------------------------------------------------------------


class TestRemoveDeadBrowsersClearsOwner:
    async def test_remove_dead_browsers_clears_dashboard_owner(self) -> None:
        """Line 213->220: dead socket is dashboard owner → clear owner, set notify_hijack_off."""
        hub = TermHub()
        now = time.time()

        owner_ws = _make_ws()
        worker_ws = _make_ws()
        worker_ws.send_text = AsyncMock()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[owner_ws] = "admin"
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = now + 300

        result = await hub.remove_dead_browsers("w1", {owner_ws})
        assert result is True  # notify_hijack_off was True

        async with hub._lock:
            st2 = hub._workers.get("w1")
            assert st2 is not None
            assert st2.hijack_owner is None


# ---------------------------------------------------------------------------
# hijack/hub/ownership.py line 225->227 — remove_dead_browsers recheck finds hijack
# ---------------------------------------------------------------------------


class TestRemoveDeadBrowsersRecheckFindsHijack:
    async def test_remove_dead_browsers_recheck_blocks_resume(self) -> None:
        """Lines 225->227: notify_hijack_off=True but recheck finds is_hijacked → False."""
        hub = TermHub()
        now = time.time()

        owner_ws = _make_ws()
        worker_ws = _make_ws()
        worker_ws.send_text = AsyncMock()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[owner_ws] = "admin"
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = now + 300

        # Install a REST session between the first lock release and the recheck
        original_acquire = hub._lock.acquire
        acquire_count = 0

        async def _inject_on_recheck() -> bool:
            nonlocal acquire_count
            acquire_count += 1
            result = await original_acquire()
            if acquire_count == 2:
                st = hub._workers.get("w1")
                if st is not None and st.hijack_session is None:
                    st.hijack_session = HijackSession(
                        hijack_id="injected",
                        owner="concurrent",
                        acquired_at=now,
                        lease_expires_at=now + 300,
                        last_heartbeat=now,
                    )
            return result  # type: ignore[return-value]

        hub._lock.acquire = _inject_on_recheck  # type: ignore[method-assign]

        resume_calls: list = []
        original_send = hub.send_worker

        async def _capture_send(wid: str, msg: dict) -> bool:
            if msg.get("action") == "resume":
                resume_calls.append(msg)
            return await original_send(wid, msg)

        hub.send_worker = _capture_send  # type: ignore[method-assign]

        result = await hub.remove_dead_browsers("w1", {owner_ws})
        # notify_hijack_off was flipped to False by recheck
        assert result is False
        assert len(resume_calls) == 0


# ---------------------------------------------------------------------------
# hijack/hub/ownership.py line 306 — release_rest_hijack: st is None or no session
# ---------------------------------------------------------------------------


class TestReleaseRestHijackNotFound:
    async def test_release_rest_hijack_worker_not_found(self) -> None:
        """Line 306: st is None → return False, False."""
        hub = TermHub()
        was_released, should_resume = await hub.release_rest_hijack("nonexistent", "any-id")
        assert was_released is False
        assert should_resume is False

    async def test_release_rest_hijack_wrong_hijack_id(self) -> None:
        """Line 306: hijack_session.hijack_id != hijack_id → return False, False."""
        hub = TermHub()
        now = time.time()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.hijack_session = HijackSession(
                hijack_id="correct-id",
                owner="op",
                acquired_at=now,
                lease_expires_at=now + 300,
                last_heartbeat=now,
            )

        was_released, should_resume = await hub.release_rest_hijack("w1", "wrong-id")
        assert was_released is False
        assert should_resume is False

    async def test_release_rest_hijack_no_session(self) -> None:
        """Line 306: st.hijack_session is None → return False, False."""
        hub = TermHub()

        async with hub._lock:
            hub._workers.setdefault("w1", WorkerTermState())

        was_released, should_resume = await hub.release_rest_hijack("w1", "any-id")
        assert was_released is False
        assert should_resume is False


# ---------------------------------------------------------------------------
# Async setup helper used by TestRestSendKeysTooLong (kept here for grouping)
# ---------------------------------------------------------------------------


async def _setup_hijack_session(hub: TermHub, worker_id: str, hid: str, now: float) -> None:
    async with hub._lock:
        st = hub._workers.setdefault(worker_id, WorkerTermState())
        st.worker_ws = _make_ws()
        st.worker_ws.send_text = AsyncMock()
        st.hijack_session = HijackSession(
            hijack_id=hid,
            owner="tester",
            acquired_at=now,
            lease_expires_at=now + 300,
            last_heartbeat=now,
        )


# ---------------------------------------------------------------------------
# hijack/hub/ownership.py line 333 — prepare_browser_input: st is None
# ---------------------------------------------------------------------------


class TestPrepareBrowserInputStNone:
    async def test_prepare_browser_input_returns_false_when_no_worker(self) -> None:
        """Line 333: st is None → return False (in prepare_browser_input)."""
        hub = TermHub()
        ws = _make_ws()
        result = await hub.prepare_browser_input("nonexistent", ws)
        assert result is False


# ---------------------------------------------------------------------------
# hijack/hub/ownership.py is_input_open_mode: also test False case
# ---------------------------------------------------------------------------


class TestIsInputOpenModeStNone:
    async def test_is_input_open_mode_returns_false_when_no_worker(self) -> None:
        """is_input_open_mode: st is None → False."""
        hub = TermHub()
        result = await hub.is_input_open_mode("nonexistent")
        assert result is False


def _make_app(**hub_kwargs: Any) -> tuple:  # type: ignore[type-arg]
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    hub = TermHub(**hub_kwargs)
    app = FastAPI()
    app.include_router(hub.create_router())
    client = TestClient(app, raise_server_exceptions=True)
    return hub, app, client
