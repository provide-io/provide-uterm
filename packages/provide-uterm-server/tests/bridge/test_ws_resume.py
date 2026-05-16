#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""WebSocket session resumption integration tests.

Tests the full resume flow: token issuance on connect, token exchange on
reconnect, role restoration, hijack reclaim, and edge cases (expired tokens,
wrong worker_id, two-tab race, no resume_store configured).
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.bridge.hub import InMemoryResumeStore, TermHub
from provide.uterm.client import connect_test_ws

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
        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            assert hello["resume_supported"] is True
            assert hello["resume_token"] is not None
            assert isinstance(hello["resume_token"], str)
            assert len(hello["resume_token"]) > 10

    def test_hello_no_resume_token_without_store(self) -> None:
        app, hub = make_app(role="admin")
        client = TestClient(app)
        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            assert hello["resume_supported"] is False
            assert hello["resume_token"] is None

    def test_each_connection_gets_unique_token(self) -> None:
        store = InMemoryResumeStore()
        app, hub = make_app(role="admin", resume_store=store)
        client = TestClient(app)
        tokens = []
        for _ in range(3):
            with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
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
        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        # Reconnect and resume
        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
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

        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        # Manually expire the token by revoking it (simulates TTL expiry)
        asyncio.run(store.revoke(token))
        assert asyncio.run(store.get(token)) is None

        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
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

        with connect_test_ws(client, "/ws/browser/worker-a/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        # Connect to different worker — resume should be silently ignored
        with connect_test_ws(client, "/ws/browser/worker-b/term") as ws:
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

        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        # Resume once — should succeed
        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["type"] == "hello"
            assert resumed["resumed"] is True

        # Token is now revoked
        assert asyncio.run(store.get(token)) is None

    def test_two_tab_race(self) -> None:
        """Tab A resumes with token → tab B tries same token → silently ignored."""
        store = InMemoryResumeStore()
        app, hub = make_app(role="admin", resume_store=store)
        client = TestClient(app)

        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        # Tab A resumes successfully
        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws_a:
            _read_initial(ws_a)
            ws_a.send_json({"type": "resume", "token": token})
            resumed = ws_a.receive_json()
            assert resumed["resumed"] is True

            # Token is now revoked — tab B cannot use it
            assert asyncio.run(store.get(token)) is None

    def test_first_message_not_resume(self) -> None:
        """First message is NOT resume → processed as normal."""
        store = InMemoryResumeStore()
        app, hub = make_app(role="admin", resume_store=store)
        client = TestClient(app)

        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
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

        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": ""})
            ws.send_json({"type": "ping"})
            pong = ws.receive_json()
            assert pong["type"] == "pong"

    def test_non_string_resume_token_is_ignored(self) -> None:
        """Malformed resume payloads must not crash the websocket handler."""
        store = InMemoryResumeStore()
        app, hub = make_app(role="admin", resume_store=store)
        client = TestClient(app)

        with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": {"unexpected": "object"}})
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

        with connect_test_ws(client, f"/ws/worker/{WID}/term") as worker:
            _read_worker_snapshot_req(worker)
            worker.send_json({"type": "snapshot", "screen": "ready", "ts": 1.0})

            # Connect browser, get token
            with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
                hello, _ = _read_initial(ws)
                token = hello["resume_token"]
                snapshot = ws.receive_json()
                assert snapshot["type"] == "snapshot"

            # Simulate that this session was a hijack owner at disconnect
            asyncio.run(store.mark_hijack_owner(token, True))

            # Reconnect and resume — should reclaim hijack (no active hijack exists)
            with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws2:
                _read_initial(ws2)
                snapshot2 = ws2.receive_json()
                assert snapshot2["type"] == "snapshot"
                ws2.send_json({"type": "resume", "token": token})
                pause = worker.receive_json()
                assert pause["type"] == "control"
                assert pause["action"] == "pause"
                resumed = ws2.receive_json()
                assert resumed["type"] == "hello"
                assert resumed["resumed"] is True
                assert resumed["hijacked_by_me"] is True
                hs = ws2.receive_json()
                assert hs["type"] == "hijack_state"
                assert hs["owner"] == "me"

    def test_resume_repauses_worker_when_hijack_is_reclaimed(self) -> None:
        """A resumed hijack should restore the worker-side paused state."""
        store = InMemoryResumeStore()
        app, hub = make_app(role="admin", resume_store=store)
        client = TestClient(app)

        with connect_test_ws(client, f"/ws/worker/{WID}/term") as worker:
            _read_worker_snapshot_req(worker)
            worker.send_json({"type": "snapshot", "screen": "ready", "ts": 1.0})

            with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws:
                hello, _ = _read_initial(ws)
                token = hello["resume_token"]
                snapshot = ws.receive_json()
                assert snapshot["type"] == "snapshot"
                ws.send_json({"type": "hijack_request"})
                pause = worker.receive_json()
                assert pause["type"] == "control"
                assert pause["action"] == "pause"
                state = ws.receive_json()
                assert state["type"] == "hijack_state"
                assert state["owner"] == "me"

            released = worker.receive_json()
            assert released["type"] == "control"
            assert released["action"] == "resume"

            with connect_test_ws(client, f"/ws/browser/{WID}/term") as ws2:
                _read_initial(ws2)
                snapshot2 = ws2.receive_json()
                assert snapshot2["type"] == "snapshot"
                ws2.send_json({"type": "resume", "token": token})
                reparsed_pause = worker.receive_json()
                assert reparsed_pause["type"] == "control"
                assert reparsed_pause["action"] == "pause"
                resumed = ws2.receive_json()
                assert resumed["type"] == "hello"
                assert resumed["resumed"] is True
                assert resumed["hijacked_by_me"] is True
                hs = ws2.receive_json()
                assert hs["type"] == "hijack_state"
                assert hs["owner"] == "me"
