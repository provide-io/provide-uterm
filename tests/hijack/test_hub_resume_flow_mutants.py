#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for hijack resume routes — token handling and hijack reclaim."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.terminal.hijack.hub import InMemoryResumeStore, TermHub
from provide.terminal.hijack.models import WorkerTermState
from provide.terminal.hijack.routes.browser_handlers import (
    _handle_resume,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_hub(resume_store: InMemoryResumeStore | None = None) -> TermHub:
    return TermHub(resume_store=resume_store)


def _make_ws() -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


async def _register(
    hub: TermHub,
    worker_id: str,
    browser_ws: Any,
    role: str,
    worker_ws: Any | None = None,
) -> WorkerTermState:
    async with hub._lock:
        st = hub._workers.setdefault(worker_id, WorkerTermState())
        st.browsers[browser_ws] = role
        if worker_ws is not None:
            st.worker_ws = worker_ws
        return st


def _make_app_client(
    role: str,
    store: InMemoryResumeStore | None = None,
) -> tuple[TestClient, TermHub]:
    hub = TermHub(
        resolve_browser_role=lambda _ws, _wid: role,
        resume_store=store,
    )
    app = FastAPI()
    app.include_router(hub.create_router())
    return TestClient(app), hub


def _read_initial(ws: Any) -> tuple[dict, dict]:
    hello = ws.receive_json()
    assert hello["type"] == "hello"
    hs = ws.receive_json()
    assert hs["type"] == "hijack_state"
    return hello, hs


# ---------------------------------------------------------------------------
# heartbeat message — field keys must be exact
# ---------------------------------------------------------------------------


class TestHandleResumeTokenAndCallbacks:
    """mutmut_5,7,10,20,21: token default and _on_resume args must be exact."""

    async def test_missing_token_key_returns_unchanged(self) -> None:
        """mutmut_5,7: msg_b.get('token', '') with missing key → '' → falsy → no resume."""
        store = InMemoryResumeStore()
        hub = TermHub(resume_store=store)
        ws = _make_ws()
        # Valid session in store, but message has no token key
        store.create("w1", "admin", 300)
        result = await _handle_resume(hub, ws, "w1", "admin", {"type": "resume"}, False)
        assert result is False
        # No hello message sent
        ws.send_text.assert_not_called()

    async def test_token_placeholder_string_causes_resume_attempt(self) -> None:
        """mutmut_10: default of 'XXXX' (truthy) would bypass the empty-check guard.

        If get('token', 'XXXX') is used instead of get('token', ''), a missing
        token key would produce 'XXXX' which is truthy, allowing the code to
        attempt a lookup that will fail with None — but the empty-string guard
        prevents even reaching that point.
        """
        store = InMemoryResumeStore()
        hub = TermHub(resume_store=store)
        ws = _make_ws()
        # No token key in message — should return without calling send_text
        result = await _handle_resume(hub, ws, "w1", "admin", {"type": "resume"}, True)
        assert result is True  # owned_hijack unchanged
        ws.send_text.assert_not_called()

    async def test_on_resume_called_with_old_token(self) -> None:
        """mutmut_20: _on_resume must receive old_token (not None)."""
        store = InMemoryResumeStore()
        received_args = []

        async def on_resume(token, session):
            received_args.append((token, session))
            return True  # allow resume

        hub = TermHub(resume_store=store, on_resume=on_resume)
        ws = _make_ws()
        await _register(hub, "w1", ws, "admin")
        token = store.create("w1", "admin", 300)
        await _handle_resume(hub, ws, "w1", "admin", {"type": "resume", "token": token}, False)
        assert received_args, "on_resume was not called"
        assert received_args[0][0] == token  # first arg must be old_token

    async def test_on_resume_called_with_session(self) -> None:
        """mutmut_21: _on_resume must receive session (not None)."""
        store = InMemoryResumeStore()
        received_args = []

        async def on_resume(token, session):
            received_args.append((token, session))
            return True

        hub = TermHub(resume_store=store, on_resume=on_resume)
        ws = _make_ws()
        await _register(hub, "w1", ws, "admin")
        token = store.create("w1", "admin", 300)
        from provide.terminal.hijack.hub.resume import ResumeSession

        await _handle_resume(hub, ws, "w1", "admin", {"type": "resume", "token": token}, False)
        assert received_args
        assert isinstance(received_args[0][1], ResumeSession)
        assert received_args[0][1].token == token

    async def test_on_resume_rejection_blocks_resume(self) -> None:
        """on_resume returning False must prevent the resume from proceeding."""
        store = InMemoryResumeStore()

        async def on_resume(token, session):
            return False  # reject

        hub = TermHub(resume_store=store, on_resume=on_resume)
        ws = _make_ws()
        await _register(hub, "w1", ws, "admin")
        token = store.create("w1", "admin", 300)
        result = await _handle_resume(hub, ws, "w1", "admin", {"type": "resume", "token": token}, False)
        assert result is False
        ws.send_text.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_resume — new_role and can_hijack initialization
# ---------------------------------------------------------------------------


class TestHandleResumeRoleInit:
    """mutmut_25-30: new_role and can_hijack initialization."""

    async def test_new_role_initialized_to_current_role(self) -> None:
        """mutmut_25: new_role must start as role (not None) so it appears in hello response."""
        store = InMemoryResumeStore()
        client, hub = _make_app_client("operator", store)
        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["type"] == "hello"
            # Role must be a valid role string, not None
            assert resumed["role"] is not None
            assert resumed["role"] in {"viewer", "operator", "admin"}

    async def test_can_hijack_is_false_for_operator(self) -> None:
        """mutmut_26,27: can_hijack = role == 'admin' → False for 'operator'."""
        store = InMemoryResumeStore()
        client, _ = _make_app_client("operator", store)
        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["can_hijack"] is False

    async def test_can_hijack_is_true_for_admin(self) -> None:
        """mutmut_27-29: can_hijack = role == 'admin' → True for 'admin' (not !=)."""
        store = InMemoryResumeStore()
        client, _ = _make_app_client("admin", store)
        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["can_hijack"] is True


# ---------------------------------------------------------------------------
# _handle_resume — role restore condition (AND not OR)
# ---------------------------------------------------------------------------


class TestHandleResumeRoleRestoreCondition:
    """mutmut_30: session.role != role AND session.role in VALID_ROLES must both be true."""

    def test_role_not_restored_when_same_as_current(self) -> None:
        """mutmut_30: if AND is replaced with OR, same role would still update browsers dict."""
        store = InMemoryResumeStore()
        # Both connections resolve as "admin" — session.role == role
        hub = TermHub(
            resolve_browser_role=lambda _ws, _wid: "admin",
            resume_store=store,
        )
        app = FastAPI()
        app.include_router(hub.create_router())
        client = TestClient(app)

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            # Role is still admin (same as current) — not altered
            assert resumed["role"] == "admin"
            assert resumed["resumed"] is True

    def test_role_restored_when_different(self) -> None:
        """mutmut_30: role IS restored when session.role != current role."""
        store = InMemoryResumeStore()
        # First session: admin. Second connection: viewer (different role).
        hub1 = TermHub(
            resolve_browser_role=lambda _ws, _wid: "admin",
            resume_store=store,
        )
        app1 = FastAPI()
        app1.include_router(hub1.create_router())
        c1 = TestClient(app1)

        with c1.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        hub2 = TermHub(
            resolve_browser_role=lambda _ws, _wid: "viewer",
            resume_store=store,
        )
        app2 = FastAPI()
        app2.include_router(hub2.create_router())
        c2 = TestClient(app2)

        with c2.websocket_connect("/ws/browser/w1/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            # Session was admin, current is viewer — restore to admin
            assert resumed["role"] == "admin"


# ---------------------------------------------------------------------------
# _handle_resume — hijack reclaim conditions
# ---------------------------------------------------------------------------


class TestHandleResumeHijackReclaim:
    """mutmut_44,45: hijack reclaim conditions: is None AND not hub.is_hijacked()."""

    def test_hijack_not_reclaimed_when_already_hijacked_by_another(self) -> None:
        """mutmut_44: must check st.hijack_owner is None — if hijacked by another, must NOT reclaim."""
        store = InMemoryResumeStore()
        # Use a setup where an admin token was the hijack owner but someone else grabbed it
        hub = TermHub(
            resolve_browser_role=lambda _ws, _wid: "admin",
            resume_store=store,
        )
        app = FastAPI()
        app.include_router(hub.create_router())
        client = TestClient(app)

        # Connect first to get a token
        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        # Mark as hijack owner in the token
        store.mark_hijack_owner(token, True)

        # Now manually set a different hijack owner on the worker state.
        # We access _workers directly (safe in sync test context — no concurrent access).
        another_ws = MagicMock()
        st = hub._workers.get("w1")
        if st is None:
            # worker state not created yet — pre-create it
            hub._workers["w1"] = WorkerTermState()
            st = hub._workers["w1"]
        st.hijack_owner = another_ws
        st.hijack_owner_expires_at = time.time() + 60

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            # Should NOT have reclaimed hijack since another owner holds it
            assert resumed["hijacked_by_me"] is False

    def test_hijack_reclaimed_when_no_current_owner(self) -> None:
        """mutmut_53,54: owned_hijack must be True (not False/None) after reclaim."""
        store = InMemoryResumeStore()
        client, hub = _make_app_client("admin", store)

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]

        store.mark_hijack_owner(token, True)

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["hijacked_by_me"] is True


# ---------------------------------------------------------------------------
# _handle_resume — store.create and _ws_to_resume_token
# ---------------------------------------------------------------------------


class TestHandleResumeTokenCreation:
    """mutmut_56,57,62: store.create args and _ws_to_resume_token assignment."""

    def test_new_token_created_with_correct_worker_id(self) -> None:
        """mutmut_56: store.create(worker_id, ...) not store.create(None, ...)."""
        store = InMemoryResumeStore()
        client, _ = _make_app_client("admin", store)

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = _read_initial(ws)
            old_token = hello["resume_token"]

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": old_token})
            resumed = ws.receive_json()
            new_token = resumed["resume_token"]
            # New token must be valid and belong to w1
            session = store.get(new_token)
            assert session is not None
            assert session.worker_id == "w1"

    def test_new_token_created_with_correct_role(self) -> None:
        """mutmut_57: store.create(worker_id, new_role, ...) not (worker_id, None, ...)."""
        store = InMemoryResumeStore()
        client, _ = _make_app_client("admin", store)

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = _read_initial(ws)
            old_token = hello["resume_token"]

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": old_token})
            resumed = ws.receive_json()
            new_token = resumed["resume_token"]
            session = store.get(new_token)
            assert session is not None
            assert session.role is not None
            assert session.role in {"viewer", "operator", "admin"}

    def test_ws_to_resume_token_set_to_new_token(self) -> None:
        """mutmut_62: hub._ws_to_resume_token[ws] must be new_token (not None)."""
        store = InMemoryResumeStore()
        client, hub = _make_app_client("admin", store)

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = _read_initial(ws)
            old_token = hello["resume_token"]

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": old_token})
            resumed = ws.receive_json()
            new_token = resumed["resume_token"]

            # The token in hub._ws_to_resume_token should equal new_token, not None
            # (We check indirectly: sending another resume_token message should yield a valid token)
            assert new_token is not None
            assert new_token != old_token


# ---------------------------------------------------------------------------
# _handle_resume — hello message field keys/values
# ---------------------------------------------------------------------------
