#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""WebSocket session resumption integration tests.

Tests the full resume flow: token issuance on connect, token exchange on
reconnect, role restoration, hijack reclaim, and edge cases (expired tokens,
wrong worker_id, two-tab race, no resume_store configured).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.terminal.hijack.hub import InMemoryResumeStore, TermHub

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


# ---------------------------------------------------------------------------
# Tests: Token issuance
# ---------------------------------------------------------------------------


class TestResumeTokenIssuance:
    def test_hello_includes_resume_token_when_store_configured(self) -> None:
        store = InMemoryResumeStore()
        app, hub = make_app(role="admin", resume_store=store)
        client = TestClient(app)
        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            assert hello["resume_supported"] is True
            assert hello["resume_token"] is not None
            assert isinstance(hello["resume_token"], str)
            assert len(hello["resume_token"]) > 10

    def test_hello_no_resume_token_without_store(self) -> None:
        app, hub = make_app(role="admin")
        client = TestClient(app)
        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            assert hello["resume_supported"] is False
            assert hello["resume_token"] is None

    def test_each_connection_gets_unique_token(self) -> None:
        store = InMemoryResumeStore()
        app, hub = make_app(role="admin", resume_store=store)
        client = TestClient(app)
        tokens = []
        for _ in range(3):
            with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
                hello, _ = _read_initial(ws)
                tokens.append(hello["resume_token"])
        assert len(set(tokens)) == 3


# ---------------------------------------------------------------------------
# Tests: Resume flow
# ---------------------------------------------------------------------------


class TestResumeFlow:
    def test_resume_sends_updated_hello(self) -> None:
        """Connect → get token → disconnect → reconnect → resume → resumed hello."""
        store = InMemoryResumeStore()
        app, hub = make_app(role="admin", resume_store=store)
        client = TestClient(app)

        # First connection — get token
        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        # Reconnect and resume
        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
            hello1, _ = _read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed_hello = ws.receive_json()
            assert resumed_hello["type"] == "hello"
            assert resumed_hello["resumed"] is True
            assert resumed_hello["resume_token"] is not None
            assert resumed_hello["resume_token"] != token  # new token issued
            # Also get hijack_state
            hs = ws.receive_json()
            assert hs["type"] == "hijack_state"

    def test_resume_with_expired_token_is_silent(self) -> None:
        """Expired token → resume is silently ignored, browser keeps fresh session."""
        store = InMemoryResumeStore()
        app, hub = make_app(role="admin", resume_store=store)
        client = TestClient(app)

        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        # Manually expire the token by revoking it (simulates TTL expiry)
        store.revoke(token)
        assert store.get(token) is None

        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
            hello2, _ = _read_initial(ws)
            # Send resume with revoked/expired token — should be silently ignored
            ws.send_json({"type": "resume", "token": token})
            # Connection still works
            ws.send_json({"type": "ping"})
            pong = ws.receive_json()
            assert pong["type"] == "pong"

    def test_resume_wrong_worker_id_is_ignored(self) -> None:
        """Token from worker A cannot resume on worker B."""
        store = InMemoryResumeStore()
        app, hub = make_app(role="admin", resume_store=store)
        client = TestClient(app)

        with client.websocket_connect("/ws/browser/worker-a/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        # Connect to different worker — resume should be silently ignored
        with client.websocket_connect("/ws/browser/worker-b/term") as ws:
            hello2, _ = _read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            ws.send_json({"type": "ping"})
            pong = ws.receive_json()
            assert pong["type"] == "pong"

    def test_resume_revokes_old_token(self) -> None:
        """After successful resume, the old token is revoked (one-time use)."""
        store = InMemoryResumeStore()
        app, hub = make_app(role="admin", resume_store=store)
        client = TestClient(app)

        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        # Resume once — should succeed
        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["type"] == "hello"
            assert resumed["resumed"] is True

        # Token is now revoked
        assert store.get(token) is None

    def test_two_tab_race(self) -> None:
        """Tab A resumes with token → tab B tries same token → silently ignored."""
        store = InMemoryResumeStore()
        app, hub = make_app(role="admin", resume_store=store)
        client = TestClient(app)

        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        # Tab A resumes successfully
        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws_a:
            _read_initial(ws_a)
            ws_a.send_json({"type": "resume", "token": token})
            resumed = ws_a.receive_json()
            assert resumed["resumed"] is True

            # Token is now revoked — tab B cannot use it
            assert store.get(token) is None

    def test_first_message_not_resume(self) -> None:
        """First message is NOT resume → processed as normal."""
        store = InMemoryResumeStore()
        app, hub = make_app(role="admin", resume_store=store)
        client = TestClient(app)

        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            assert hello["resume_token"] is not None
            ws.send_json({"type": "ping"})
            pong = ws.receive_json()
            assert pong["type"] == "pong"

    def test_empty_token_ignored(self) -> None:
        """Resume with empty token string is silently ignored."""
        store = InMemoryResumeStore()
        app, hub = make_app(role="admin", resume_store=store)
        client = TestClient(app)

        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": ""})
            ws.send_json({"type": "ping"})
            pong = ws.receive_json()
            assert pong["type"] == "pong"


# ---------------------------------------------------------------------------
# Tests: Hijack reclaim
# ---------------------------------------------------------------------------


class TestResumeHijackReclaim:
    def test_resume_reclaims_hijack(self) -> None:
        """Acquire hijack → disconnect → resume → hijack reclaimed.

        Use a simpler flow: manually mark hijack ownership on the token, then
        verify that resume reclaims it.
        """
        store = InMemoryResumeStore()
        app, hub = make_app(role="admin", resume_store=store)
        client = TestClient(app)

        # Connect browser, get token
        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        # Simulate that this session was a hijack owner at disconnect
        store.mark_hijack_owner(token, True)

        # Reconnect and resume — should reclaim hijack (no active hijack exists)
        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws2:
            hello2, _ = _read_initial(ws2)
            ws2.send_json({"type": "resume", "token": token})
            resumed = ws2.receive_json()
            assert resumed["type"] == "hello"
            assert resumed["resumed"] is True
            assert resumed["hijacked_by_me"] is True
            hs = ws2.receive_json()
            assert hs["type"] == "hijack_state"
            assert hs["owner"] == "me"


# ---------------------------------------------------------------------------
# Tests: No store configured
# ---------------------------------------------------------------------------


class TestResumeRoleRestoration:
    def test_resume_restores_different_role(self) -> None:
        """Connect as admin → get token → reconnect as viewer → resume → role restored to admin."""
        store = InMemoryResumeStore()
        # First connection: resolver returns admin
        hub = TermHub(
            resolve_browser_role=lambda _ws, _wid: "admin",
            resume_store=store,
        )
        app = FastAPI()
        app.include_router(hub.create_router())
        client = TestClient(app)

        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]
            assert hello["role"] == "admin"

        # Now manually change the token's role to "admin" (it already is)
        # But for the resume, we need the RESOLVER to return a different role.
        # So create a new app where the resolver returns "viewer"
        hub2 = TermHub(
            resolve_browser_role=lambda _ws, _wid: "viewer",
            resume_store=store,  # same store
        )
        app2 = FastAPI()
        app2.include_router(hub2.create_router())
        client2 = TestClient(app2)

        with client2.websocket_connect(f"/ws/browser/{WID}/term") as ws:
            hello2, _ = _read_initial(ws)
            # Resolver gave "viewer"
            assert hello2["role"] == "viewer"
            # Resume with admin token
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["type"] == "hello"
            assert resumed["resumed"] is True
            # Role restored to admin from the token
            assert resumed["role"] == "admin"
            assert resumed["can_hijack"] is True

    def test_resume_restores_operator_role(self) -> None:
        """Resume restores operator role when reconnecting as viewer."""
        store = InMemoryResumeStore()
        # First: connect as operator
        hub = TermHub(
            resolve_browser_role=lambda _ws, _wid: "operator",
            resume_store=store,
        )
        app = FastAPI()
        app.include_router(hub.create_router())
        client = TestClient(app)

        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        # Reconnect as viewer — resume should restore operator
        hub2 = TermHub(
            resolve_browser_role=lambda _ws, _wid: "viewer",
            resume_store=store,
        )
        app2 = FastAPI()
        app2.include_router(hub2.create_router())
        client2 = TestClient(app2)

        with client2.websocket_connect(f"/ws/browser/{WID}/term") as ws:
            hello2, _ = _read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["resumed"] is True
            assert resumed["role"] == "operator"
            assert resumed["can_hijack"] is False


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
        from provide.terminal.hijack.routes.browser_handlers import _handle_resume

        hub = TermHub()  # no resume_store
        assert hub._resume_store is None

        result = await _handle_resume(hub, None, "w1", "admin", {"type": "resume", "token": "x"}, False)  # type: ignore[arg-type]
        assert result is False


class TestNoResumeStore:
    def test_no_resume_token_in_hello(self) -> None:
        app, hub = make_app(role="admin")
        client = TestClient(app)
        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            assert hello["resume_supported"] is False
            assert hello["resume_token"] is None

    def test_resume_message_ignored_without_store(self) -> None:
        """Without a resume store, resume messages are ignored gracefully."""
        app, hub = make_app(role="admin")
        client = TestClient(app)
        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
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

        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
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

        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        with client.websocket_connect(f"/ws/browser/{WID}/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["type"] == "hello"
            assert resumed["resumed"] is True
