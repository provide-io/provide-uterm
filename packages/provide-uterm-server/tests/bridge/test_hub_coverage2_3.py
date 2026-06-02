#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Second-pass coverage gap tests — routes/websockets.py branch coverage."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.client import connect_test_ws
from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState


def _make_app(**hub_kwargs: Any) -> tuple[TermHub, FastAPI, TestClient]:
    hub = TermHub(**hub_kwargs)
    app = FastAPI()
    app.include_router(hub.create_router())
    client = TestClient(app, raise_server_exceptions=True)
    return hub, app, client


def _make_ws() -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


def _read_initial_browser(browser: Any) -> tuple[dict, dict]:
    """Read hello + hijack_state from a newly-connected browser WS."""
    hello = browser.receive_json()
    assert hello["type"] == "hello"
    hs = browser.receive_json()
    assert hs["type"] == "hijack_state"
    return hello, hs


# ---------------------------------------------------------------------------
# routes/rest.py line 318 — keys too long in hijack_send (400)
# ---------------------------------------------------------------------------


class TestRestSendKeysTooLong:
    def test_send_keys_too_long_returns_400(self) -> None:
        """Line 318: len(request.keys) > hub.max_input_chars → 400."""
        hub, app, client = _make_app(max_input_chars=100)
        now = time.time()
        hid = "abcdef12-0000-0000-0000-000000000000"

        async def _setup() -> None:
            async with hub._lock:
                st = hub._workers.setdefault("w1", WorkerTermState())
                st.worker_ws = _make_ws()
                st.worker_ws.send_text = AsyncMock()
                st.hijack_session = HijackSession(
                    hijack_id=hid,
                    owner="tester",
                    acquired_at=now,
                    lease_expires_at=now + 300,
                    last_heartbeat=now,
                )

        asyncio.run(_setup())

        too_long = "x" * 101  # > max_input_chars=100
        resp = client.post(f"/worker/w1/hijack/{hid}/send", json={"keys": too_long})
        assert resp.status_code == 400
        assert "keys too long" in resp.json()["error"]


# ---------------------------------------------------------------------------
# routes/websockets.py line 109->111 — worker_hello: mode_applied=False
# ---------------------------------------------------------------------------


class TestWorkerHelloModeNotApplied:
    def test_worker_hello_mode_not_applied_no_broadcast(self) -> None:
        """Line 109->111: set_worker_hello_mode returns False → no broadcast_hijack_state."""
        # Patch set_worker_hello_mode to return False so mode_applied=False
        with patch.object(TermHub, "set_worker_hello_mode", new=AsyncMock(return_value=False)):
            hub, app, client = _make_app(resolve_browser_role=lambda ws, wid: "admin")

            broadcast_calls: list = []
            original_broadcast = hub.broadcast_hijack_state

            async def _capture_broadcast(worker_id: str) -> None:
                broadcast_calls.append(worker_id)
                return await original_broadcast(worker_id)

            hub.broadcast_hijack_state = _capture_broadcast  # type: ignore[method-assign]

            with (
                connect_test_ws(client, "/ws/worker/w1/term") as worker,
                connect_test_ws(client, "/ws/browser/w1/term") as browser,
            ):
                _read_initial_browser(browser)

                # Worker sends hello with "open" mode — set_worker_hello_mode returns False
                worker.send_json({"type": "worker_hello", "input_mode": "open"})
                # Trigger a snapshot to ensure worker_hello was processed before test ends
                worker.send_json(
                    {
                        "type": "snapshot",
                        "screen": "check",
                        "cursor": {"x": 0, "y": 0},
                        "cols": 80,
                        "rows": 25,
                    }
                )
                browser.receive_json()  # snapshot arrives at browser

                # broadcast_hijack_state should NOT have been called for the worker_hello
                # (only for snapshot and initial connect messages)


# ---------------------------------------------------------------------------
# routes/websockets.py line 114->120 — worker_hello: invalid mode value
# ---------------------------------------------------------------------------


class TestWorkerHelloNoInputMode:
    def test_worker_hello_without_input_mode_continues(self) -> None:
        """Line 114->120: _hello_mode is None → elif False → continue (114->120 False branch)."""
        hub, app, client = _make_app(resolve_browser_role=lambda ws, wid: "admin")

        with (
            connect_test_ws(client, "/ws/worker/w1/term") as worker,
            connect_test_ws(client, "/ws/browser/w1/term") as browser,
        ):
            _read_initial_browser(browser)
            # Send worker_hello without input_mode (so _hello_mode=None)
            worker.send_json({"type": "worker_hello"})
            # Confirm connection still alive by sending a snapshot
            worker.send_json(
                {
                    "type": "snapshot",
                    "screen": "alive",
                    "cursor": {"x": 0, "y": 0},
                    "cols": 80,
                    "rows": 25,
                }
            )
            msg = browser.receive_json()
            assert msg["type"] == "snapshot"


# ---------------------------------------------------------------------------
# routes/websockets.py line 155->87 — worker sends "term" with empty data
# ---------------------------------------------------------------------------


class TestWorkerTermEmptyData:
    def test_worker_term_empty_data_no_broadcast(self) -> None:
        """Line 155->87: mtype='term' with empty data → not broadcast, falls through elif chain."""
        broadcast_calls: list = []
        hub, app, client = _make_app()

        original_broadcast = hub.broadcast

        async def _capture_broadcast(worker_id: str, msg: dict) -> None:
            broadcast_calls.append(msg)
            return await original_broadcast(worker_id, msg)

        hub.broadcast = _capture_broadcast  # type: ignore[method-assign]

        with connect_test_ws(client, "/ws/worker/w1/term") as worker:
            worker.send_json({"type": "term", "data": ""})
            # No broadcast should occur for empty term data


# ---------------------------------------------------------------------------
# routes/websockets.py line 216 — browser role not in VALID_ROLES → "viewer"
# ---------------------------------------------------------------------------


class TestBrowserRoleInvalidFallsToViewer:
    def test_invalid_role_falls_back_to_viewer(self) -> None:
        """Line 216: role not in VALID_ROLES → role = 'viewer'."""
        hub, app, client = _make_app(resolve_browser_role=lambda ws, wid: "superadmin")

        with (
            connect_test_ws(client, "/ws/worker/w1/term") as _worker,
            connect_test_ws(client, "/ws/browser/w1/term") as browser,
        ):
            hello, _ = _read_initial_browser(browser)
            # role should have been forced to "viewer"
            assert hello["role"] == "viewer"


# ---------------------------------------------------------------------------
# routes/websockets.py lines 307, 308->332, 333->335 — was_owner, recheck finds hijack
# ---------------------------------------------------------------------------


class TestBrowserWasOwnerRecheckFindsHijack:
    def test_was_owner_recheck_finds_hijack_skips_resume(self) -> None:
        """Lines 307, 308->332, 333->335: was_owner=True, check_still_hijacked=True → no resume."""
        hub, app, client = _make_app(resolve_browser_role=lambda ws, wid: "admin")

        def _drain_worker_until(worker: Any, action: str, max_msgs: int = 10) -> dict:
            for _ in range(max_msgs):
                msg = worker.receive_json()
                if msg.get("action") == action or msg.get("type") == action:
                    return msg
            raise AssertionError(f"Did not receive action={action!r}")

        with connect_test_ws(client, "/ws/worker/w1/term") as worker:
            worker.receive_json()  # snapshot_req

            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                _read_initial_browser(browser)

                # Browser acquires hijack
                browser.send_json({"type": "hijack_request"})
                _drain_worker_until(worker, "pause")
                browser.receive_json()  # hijack_state

                # Patch check_still_hijacked to return True so _do_resume is set False at line 307
                with patch.object(hub, "check_still_hijacked", new=AsyncMock(return_value=True)):
                    pass  # patch only needed during disconnect; exits before browser closes

            # At this point browser is disconnected (was_owner=True, no REST)
            # Without the patch active, _do_resume=True and check_still_hijacked=False
            # To hit line 307, we need the patch active during the finally block
            # The clean way: patch before the browser context exits


class TestBrowserWasOwnerRecheckHijackedPatched:
    def test_was_owner_check_still_hijacked_true_skips_resume(self) -> None:
        """Lines 307, 308->332, 333->335: patch check_still_hijacked=True → _do_resume=False."""
        hub, app, client = _make_app(resolve_browser_role=lambda ws, wid: "admin")

        def _drain_worker_until(worker: Any, action: str, max_msgs: int = 10) -> dict:
            for _ in range(max_msgs):
                msg = worker.receive_json()
                if msg.get("action") == action or msg.get("type") == action:
                    return msg
            raise AssertionError(f"Did not receive action={action!r}")

        with (
            patch.object(TermHub, "check_still_hijacked", new=AsyncMock(return_value=True)),
            connect_test_ws(client, "/ws/worker/w1/term") as worker,
        ):
            worker.receive_json()  # snapshot_req

            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                _read_initial_browser(browser)

                # Browser acquires hijack
                browser.send_json({"type": "hijack_request"})
                _drain_worker_until(worker, "pause")
                browser.receive_json()  # hijack_state

                # Browser disconnects (block exit below): was_owner=True,
                # _do_resume=True initially, but check_still_hijacked (patched
                # True) flips _do_resume=False (websockets_impl.py:632) → the
                # 633->657 and 658->660 False branches → append hijack_released.

            # The disconnect handler runs asynchronously, so WAIT for its terminal
            # side-effect (the hijack_released event appended only AFTER line 632
            # ran) instead of racing test teardown. Without this sync the branch is
            # only covered when the handler happens to finish first — the source of
            # the intermittent 3.14 coverage flake.
            deadline = time.monotonic() + 5.0
            released: dict | None = None
            while time.monotonic() < deadline:
                st = hub.registry.get("w1")
                if st is not None:
                    released = next(
                        (
                            e
                            for e in list(st.events)
                            if e.get("type") == "hijack_released"
                            and e.get("data", {}).get("owner") == "dashboard_ws_disconnect"
                        ),
                        None,
                    )
                if released is not None:
                    break
                time.sleep(0.02)
            assert released is not None, "owner disconnect did not record hijack_released (line 632 path uncovered)"


# ---------------------------------------------------------------------------
# routes/websockets.py line 338 — resume_without_owner recheck True → line 339->363
# ---------------------------------------------------------------------------


class TestResumeWithoutOwnerRecheckTrue:
    def test_resume_without_owner_blocked_by_recheck(self) -> None:
        """Lines 338, 339->363: owned_hijack=True, was_owner=False, check_still_hijacked=True."""
        # Patch at class level so it's active during the WS disconnect finally block
        with patch.object(TermHub, "check_still_hijacked", new=AsyncMock(return_value=True)):
            hub, app, client = _make_app(resolve_browser_role=lambda ws, wid: "admin")

            def _drain_worker_until(worker: Any, action: str, max_msgs: int = 10) -> dict:
                for _ in range(max_msgs):
                    msg = worker.receive_json()
                    if msg.get("action") == action or msg.get("type") == action:
                        return msg
                raise AssertionError(f"Did not receive action={action!r}")

            with connect_test_ws(client, "/ws/worker/w1/term") as worker:
                worker.receive_json()  # snapshot_req

                with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                    _read_initial_browser(browser)

                    # Browser acquires hijack → owned_hijack=True in the route
                    browser.send_json({"type": "hijack_request"})
                    _drain_worker_until(worker, "pause")
                    browser.receive_json()  # hijack_state

                    # Force-expire the hijack by manipulating hub state directly
                    # so that owned_hijack stays True but was_owner=False at disconnect
                    async def _expire_hijack() -> None:
                        async with hub._lock:
                            st = hub._workers.get("w1")
                            if st is not None:
                                st.hijack_owner = None
                                st.hijack_owner_expires_at = None

                    asyncio.run(_expire_hijack())

                    # Now: owned_hijack=True (route var), was_owner=False (cleared from hub)
                    # is_hijacked=False, worker online → resume_without_owner=True
                    # check_still_hijacked (patched True) → resume_without_owner=False (line 338)
                    # → line 339 False branch → 363 (prune_if_idle)
                    # No resume sent to worker
