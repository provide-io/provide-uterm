#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""WebSocket session resumption integration tests (part 2).

Tests role restoration, open-mode guard, broadcast, and callback flows.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.client import connect_test_ws
from provide.uterm.server.bridge.hub import InMemoryResumeStore, TermHub

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WID = "resume-test"


def make_app(
    role: str | None = None,
    *,
    resume_store: InMemoryResumeStore | None = None,
    resume_ttl_s: float = 300,
) -> tuple[FastAPI, TermHub]:
    resolver = (lambda _ws, _wid: role) if role is not None else None
    hub = TermHub(
        resolve_browser_role=resolver,
        resume_store=resume_store,
        resume_ttl_s=resume_ttl_s,
    )
    app = FastAPI()
    app.include_router(hub.create_router())
    return app, hub


def _read_initial(browser) -> tuple[dict, dict]:
    """Read hello + hijack_state from a fresh browser connection."""
    hello = browser.receive_json()
    assert hello["type"] == "hello"
    hijack_state = browser.receive_json()
    assert hijack_state["type"] == "hijack_state"
    return hello, hijack_state


def _read_worker_snapshot_req(worker) -> dict:
    msg = worker.receive_json()
    assert msg["type"] == "snapshot_req"
    return msg


def _read_worker_control(worker, max_frames: int = 6) -> dict:
    """Return the next worker ``control`` frame, skipping any ``snapshot_req``.

    A browser connect/resume enqueues a ``snapshot_req`` to the worker whose
    ordering relative to the hijack control frame is not guaranteed; draining
    past it keeps the assertion deterministic instead of racing frame order.
    """
    for _ in range(max_frames):
        msg = worker.receive_json()
        if msg.get("type") != "snapshot_req":
            return msg
    raise AssertionError("no worker control frame after draining snapshot_req frames")


# ---------------------------------------------------------------------------
# Tests: No store configured
# ---------------------------------------------------------------------------


class TestResumeRoleRestoration:
    def test_resume_restores_different_role(self) -> None:
        """Connect as viewer → get token → reconnect as admin → resume → role stays viewer (downgrade preserved)."""
        store = InMemoryResumeStore()
        hub = TermHub(
            resolve_browser_role=lambda _ws, _wid: "viewer",
            resume_store=store,
        )
        app = FastAPI()
        app.include_router(hub.create_router())
        client = TestClient(app)

        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]
            assert hello["role"] == "viewer"

        # Reconnect as admin — stored viewer role should be preserved (not escalated to admin)
        hub2 = TermHub(
            resolve_browser_role=lambda _ws, _wid: "admin",
            resume_store=store,  # same store
        )
        app2 = FastAPI()
        app2.include_router(hub2.create_router())
        client2 = TestClient(app2)

        with connect_test_ws(client2, f"/ws/browser/{WID}/term") as ws:
            hello2, _ = _read_initial(ws)
            assert hello2["role"] == "admin"
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["type"] == "hello"
            assert resumed["resumed"] is True
            # Stored viewer role is restored (downgrade preserved; stored <= current)
            assert resumed["role"] == "viewer"
            assert resumed["can_hijack"] is False

    def test_resume_restores_operator_role(self) -> None:
        """Connect as operator → reconnect as admin → resume → role stays operator (downgrade preserved)."""
        store = InMemoryResumeStore()
        hub = TermHub(
            resolve_browser_role=lambda _ws, _wid: "operator",
            resume_store=store,
        )
        app = FastAPI()
        app.include_router(hub.create_router())
        client = TestClient(app)

        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        # Reconnect as admin — stored operator role should be preserved
        hub2 = TermHub(
            resolve_browser_role=lambda _ws, _wid: "admin",
            resume_store=store,
        )
        app2 = FastAPI()
        app2.include_router(hub2.create_router())
        client2 = TestClient(app2)

        with connect_test_ws(client2, f"/ws/browser/{WID}/term") as ws:
            hello2, _ = _read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["resumed"] is True
            assert resumed["role"] == "operator"
            assert resumed["can_hijack"] is False

    def test_resume_does_not_escalate_stored_admin_to_current_viewer(self) -> None:
        """Stored admin role is NOT restored when current role is viewer (escalation blocked)."""
        store = InMemoryResumeStore()
        hub = TermHub(
            resolve_browser_role=lambda _ws, _wid: "admin",
            resume_store=store,
        )
        app = FastAPI()
        app.include_router(hub.create_router())
        client = TestClient(app)

        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]
            assert hello["role"] == "admin"

        # Reconnect as viewer — stored admin role must NOT be granted back
        hub2 = TermHub(
            resolve_browser_role=lambda _ws, _wid: "viewer",
            resume_store=store,
        )
        app2 = FastAPI()
        app2.include_router(hub2.create_router())
        client2 = TestClient(app2)

        with connect_test_ws(client2, f"/ws/browser/{WID}/term") as ws:
            hello2, _ = _read_initial(ws)
            assert hello2["role"] == "viewer"
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["type"] == "hello"
            assert resumed["resumed"] is True
            assert resumed["role"] == "viewer"
            assert resumed["can_hijack"] is False

    def test_resume_hijack_not_reclaimed_when_role_downgraded(self) -> None:
        """was_hijack_owner=True in token but current role is viewer → hijack NOT reclaimed."""
        store = InMemoryResumeStore()
        hub = TermHub(
            resolve_browser_role=lambda _ws, _wid: "admin",
            resume_store=store,
        )
        app = FastAPI()
        app.include_router(hub.create_router())
        client = TestClient(app)

        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        # Mark the token as a former hijack owner
        asyncio.run(store.mark_hijack_owner(token, True))

        # Reconnect with viewer role — hijack must not be reclaimed
        hub2 = TermHub(
            resolve_browser_role=lambda _ws, _wid: "viewer",
            resume_store=store,
        )
        app2 = FastAPI()
        app2.include_router(hub2.create_router())
        client2 = TestClient(app2)

        with connect_test_ws(client2, f"/ws/worker/{WID}/term") as worker:
            _read_worker_snapshot_req(worker)
            worker.send_json({"type": "snapshot", "screen": "ready", "ts": 1.0})

            with connect_test_ws(client2, f"/ws/browser/{WID}/term") as ws:
                _read_initial(ws)
                ws.receive_json()  # snapshot
                ws.send_json({"type": "resume", "token": token})
                resumed = ws.receive_json()
                assert resumed["type"] == "hello"
                assert resumed["resumed"] is True
                assert resumed["can_hijack"] is False
                assert resumed["hijacked_by_me"] is False
                hs = ws.receive_json()
                assert hs["type"] == "hijack_state"
                assert hs["owner"] != "me"


class TestResumeBranchCoverage:
    @pytest.mark.asyncio()
    async def test_register_browser_state_snapshot_no_worker(self) -> None:
        """register_browser_state_snapshot returns defaults when worker not registered."""
        store = InMemoryResumeStore()
        hub = TermHub(resume_store=store)

        result = await hub.register_browser_state_snapshot("nonexistent", None)  # type: ignore[arg-type]
        assert result["is_hijacked"] is False
        assert result["hijacked_by_me"] is False
        assert result["worker_online"] is False
        assert result["input_mode"] == "hijack"

    @pytest.mark.asyncio()
    async def test_resume_with_no_store_calls_handle_resume_noop(self) -> None:
        """Calling _handle_resume with no store configured returns owned_hijack unchanged."""
        from provide.uterm.server.bridge.routes.browser_handlers import _handle_resume

        hub = TermHub()  # no resume_store
        assert hub._resume_store is None

        result = await _handle_resume(hub, None, "w1", "admin", {"type": "resume", "token": "x"}, False)  # type: ignore[arg-type]
        assert result is False


class TestNoResumeStore:
    def test_no_resume_token_in_hello(self) -> None:
        app, hub = make_app(role="admin")
        client = TestClient(app)
        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            assert hello["resume_supported"] is False
            assert hello["resume_token"] is None

    def test_resume_message_ignored_without_store(self) -> None:
        """Without a resume store, resume messages are ignored gracefully."""
        app, hub = make_app(role="admin")
        client = TestClient(app)
        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
            _read_initial(ws)
            # Send resume — should be ignored (no store configured)
            ws.send_json({"type": "resume", "token": "fake-token"})
            # Connection still works
            ws.send_json({"type": "ping"})
            pong = ws.receive_json()
            assert pong["type"] == "pong"


# ---------------------------------------------------------------------------
# Tests: on_resume callback
# ---------------------------------------------------------------------------


class TestOnResumeCallback:
    def test_on_resume_can_reject(self) -> None:
        """on_resume callback returning False blocks the resume."""
        store = InMemoryResumeStore()

        async def reject_resume(token: str, session) -> bool:
            return False

        hub = TermHub(
            resolve_browser_role=lambda _ws, _wid: "admin",
            resume_store=store,
            on_resume=reject_resume,
        )
        app = FastAPI()
        app.include_router(hub.create_router())
        client = TestClient(app)

        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            # Rejected — no resumed hello, but connection still works
            ws.send_json({"type": "ping"})
            pong = ws.receive_json()
            assert pong["type"] == "pong"

    def test_on_resume_can_accept(self) -> None:
        """on_resume callback returning True allows the resume."""
        store = InMemoryResumeStore()

        async def accept_resume(token: str, session) -> bool:
            return True

        hub = TermHub(
            resolve_browser_role=lambda _ws, _wid: "admin",
            resume_store=store,
            on_resume=accept_resume,
        )
        app = FastAPI()
        app.include_router(hub.create_router())
        client = TestClient(app)

        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["type"] == "hello"
            assert resumed["resumed"] is True


# ---------------------------------------------------------------------------
# Tests: Open-mode guard on hijack reclaim (Fix 1)
# ---------------------------------------------------------------------------


class TestResumeOpenModeGuard:
    def test_resume_does_not_reclaim_hijack_in_open_mode(self) -> None:
        """was_hijack_owner=True but input_mode='open' → hijack not reclaimed on resume."""
        store = InMemoryResumeStore()
        app, hub = make_app(role="admin", resume_store=store)
        client = TestClient(app)

        with connect_test_ws(client, f"/ws/worker/{WID}/term") as worker:
            _read_worker_snapshot_req(worker)
            worker.send_json({"type": "snapshot", "screen": "ready", "ts": 1.0})

            with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
                hello, _ = _read_initial(ws)
                token = hello["resume_token"]
                ws.receive_json()  # snapshot

            asyncio.run(store.mark_hijack_owner(token, True))

            # Switch to open mode via REST (synchronous — processed before resume attempt)
            resp = client.post(f"/worker/{WID}/input_mode", json={"input_mode": "open"})
            assert resp.status_code == 200

            with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws2:
                _read_initial(ws2)
                ws2.receive_json()  # snapshot
                ws2.send_json({"type": "resume", "token": token})
                resumed = ws2.receive_json()
                assert resumed["type"] == "hello"
                assert resumed["resumed"] is True
                # Open mode: hijack reclaim must be blocked
                assert resumed["hijacked_by_me"] is False
                hs = ws2.receive_json()
                assert hs["type"] == "hijack_state"
                assert hs["owner"] != "me"


# ---------------------------------------------------------------------------
# Tests: Broadcast to observers after hijack reclaim (Fix 4)
# ---------------------------------------------------------------------------


class TestResumeHijackReclaimBroadcast:
    def test_resume_hijack_reclaim_broadcasts_to_other_browsers(self) -> None:
        """A second browser observer sees the hijack_state update when the first reclaims on resume."""
        store = InMemoryResumeStore()
        app, hub = make_app(role="admin", resume_store=store)
        client = TestClient(app)

        with connect_test_ws(client, f"/ws/worker/{WID}/term") as worker:
            _read_worker_snapshot_req(worker)
            worker.send_json({"type": "snapshot", "screen": "ready", "ts": 1.0})

            # Browser A connects and disconnects — keeps its resume token
            with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws_a:
                hello_a, _ = _read_initial(ws_a)
                token_a = hello_a["resume_token"]
                ws_a.receive_json()  # snapshot

            asyncio.run(store.mark_hijack_owner(token_a, True))

            # Browser B is an observer that stays connected
            with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws_b:
                _read_initial(ws_b)
                ws_b.receive_json()  # snapshot

                # Browser A reconnects and resumes — reclaims hijack
                with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws_a2:
                    _read_initial(ws_a2)
                    ws_a2.receive_json()  # snapshot
                    ws_a2.send_json({"type": "resume", "token": token_a})

                    # Worker receives pause
                    pause = _read_worker_control(worker)
                    assert pause["action"] == "pause"

                    # A gets resumed hello
                    resumed = ws_a2.receive_json()
                    assert resumed["resumed"] is True
                    assert resumed["hijacked_by_me"] is True
                    ws_a2.receive_json()  # hijack_state for A

                    # B must receive a hijack_state broadcast reflecting the reclaim
                    hs_b = ws_b.receive_json()
                    assert hs_b["type"] == "hijack_state"
                    assert hs_b["hijacked"] is True
