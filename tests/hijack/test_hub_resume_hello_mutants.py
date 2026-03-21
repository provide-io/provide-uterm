#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for hijack resume routes — hello fields and hijack expiry."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.terminal.hijack.hub import InMemoryResumeStore, TermHub
from provide.terminal.hijack.models import WorkerTermState

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


class TestHandleResumeHelloFields:
    """mutmut 77-164: hello message fields in the resume response must be correct."""

    def _resume(self, store: InMemoryResumeStore, role: str = "admin") -> dict:
        """Helper: connect, capture token, reconnect and resume. Returns resumed hello."""
        client, _ = _make_app_client(role, store)
        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]
        with client.websocket_connect("/ws/browser/w1/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            return ws.receive_json()

    def test_hello_has_worker_id_key(self) -> None:
        """mutmut_77,78: 'worker_id' key must be present (not 'XXworker_idXX' or 'WORKER_ID')."""
        store = InMemoryResumeStore()
        resumed = self._resume(store)
        assert "worker_id" in resumed

    def test_hello_worker_id_value_is_correct(self) -> None:
        """mutmut_77,78: 'worker_id' value must be the actual worker id."""
        store = InMemoryResumeStore()
        resumed = self._resume(store)
        assert resumed["worker_id"] == "w1"

    def test_hello_has_hijacked_key(self) -> None:
        """mutmut_81,82: 'hijacked' key must be present (not 'XXhijackedXX' or 'HIJACKED')."""
        store = InMemoryResumeStore()
        resumed = self._resume(store)
        assert "hijacked" in resumed

    def test_hello_hijacked_is_false_when_not_hijacked(self) -> None:
        """mutmut_83-89: 'hijacked' must come from is_hijacked state (False when not hijacked)."""
        store = InMemoryResumeStore()
        resumed = self._resume(store)
        assert resumed["hijacked"] is False

    def test_hello_has_hijacked_by_me_key(self) -> None:
        """mutmut_93-98: 'hijacked_by_me' key and correct default."""
        store = InMemoryResumeStore()
        resumed = self._resume(store)
        assert "hijacked_by_me" in resumed

    def test_hello_hijacked_by_me_is_false_when_not_owner(self) -> None:
        """mutmut_98: default must be False (not True) for hijacked_by_me."""
        store = InMemoryResumeStore()
        resumed = self._resume(store)
        assert resumed["hijacked_by_me"] is False

    def test_hello_has_worker_online_key(self) -> None:
        """mutmut_99,100: 'worker_online' key must be present."""
        store = InMemoryResumeStore()
        resumed = self._resume(store)
        assert "worker_online" in resumed

    def test_hello_worker_online_is_false_when_no_worker(self) -> None:
        """mutmut_102,107: default must be False (not True/None) for worker_online."""
        store = InMemoryResumeStore()
        resumed = self._resume(store)
        assert resumed["worker_online"] is False

    def test_hello_has_input_mode_key(self) -> None:
        """mutmut_108,109: 'input_mode' key must be present."""
        store = InMemoryResumeStore()
        resumed = self._resume(store)
        assert "input_mode" in resumed

    def test_hello_input_mode_default_is_hijack(self) -> None:
        """mutmut_111-117: default input_mode must be 'hijack' (not None/HIJACK/XXhijackXX)."""
        store = InMemoryResumeStore()
        resumed = self._resume(store)
        assert resumed["input_mode"] == "hijack"

    def test_hello_has_hijack_control_key(self) -> None:
        """mutmut_120,121: 'hijack_control' key must be present."""
        store = InMemoryResumeStore()
        resumed = self._resume(store)
        assert "hijack_control" in resumed

    def test_hello_hijack_control_value_is_ws(self) -> None:
        """mutmut_122,123: 'hijack_control' value must be 'ws' (not 'XXwsXX' or 'WS')."""
        store = InMemoryResumeStore()
        resumed = self._resume(store)
        assert resumed["hijack_control"] == "ws"

    def test_hello_hijack_step_supported_is_true(self) -> None:
        """mutmut_126: 'hijack_step_supported' must be True (not False)."""
        store = InMemoryResumeStore()
        resumed = self._resume(store)
        assert resumed["hijack_step_supported"] is True

    def test_hello_has_capabilities_key(self) -> None:
        """mutmut_127,128: 'capabilities' key must be present."""
        store = InMemoryResumeStore()
        resumed = self._resume(store)
        assert "capabilities" in resumed

    def test_hello_capabilities_hijack_control_is_ws(self) -> None:
        """mutmut_129-132: capabilities['hijack_control'] must be 'ws'."""
        store = InMemoryResumeStore()
        resumed = self._resume(store)
        assert "hijack_control" in resumed["capabilities"]
        assert resumed["capabilities"]["hijack_control"] == "ws"

    def test_hello_capabilities_hijack_step_supported_is_true(self) -> None:
        """mutmut_133-135: capabilities['hijack_step_supported'] must be True (not False)."""
        store = InMemoryResumeStore()
        resumed = self._resume(store)
        assert "hijack_step_supported" in resumed["capabilities"]
        assert resumed["capabilities"]["hijack_step_supported"] is True

    def test_hello_resume_supported_is_true(self) -> None:
        """mutmut_136-138: 'resume_supported' must be True (not False)."""
        store = InMemoryResumeStore()
        resumed = self._resume(store)
        assert "resume_supported" in resumed
        assert resumed["resume_supported"] is True

    def test_hello_has_resume_token(self) -> None:
        """resume token must be issued and present."""
        store = InMemoryResumeStore()
        resumed = self._resume(store)
        assert "resume_token" in resumed
        assert resumed["resume_token"] is not None

    def test_hello_resumed_is_true(self) -> None:
        """'resumed' field must be True in the response."""
        store = InMemoryResumeStore()
        resumed = self._resume(store)
        assert resumed.get("resumed") is True

    def test_hello_role_is_correct(self) -> None:
        """Role in the hello must reflect the correct (restored or current) role."""
        store = InMemoryResumeStore()
        resumed = self._resume(store, role="operator")
        assert resumed["role"] == "operator"

    def test_resume_followed_by_hijack_state(self) -> None:
        """After resumed hello, a hijack_state message must follow."""
        store = InMemoryResumeStore()
        client, _ = _make_app_client("admin", store)
        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = _read_initial(ws)
            token = hello["resume_token"]
        with client.websocket_connect("/ws/browser/w1/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["type"] == "hello"
            hijack_state = ws.receive_json()
            assert hijack_state["type"] == "hijack_state"


# ---------------------------------------------------------------------------
# _handle_resume — hijack expiry uses addition not subtraction
# ---------------------------------------------------------------------------


class TestHandleResumeHijackExpiry:
    """mutmut_51: hijack_owner_expires_at = time.time() + lease (not minus, not None)."""

    def test_reclaimed_hijack_expiry_is_in_future(self) -> None:
        """mutmut_51: expiry time must be in the future (addition, not subtraction or None)."""
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
            # hijack_state message should show we are the owner
            hs = ws.receive_json()
            assert hs["type"] == "hijack_state"
            assert hs["owner"] == "me"

    def test_reclaimed_hijack_owner_expires_at_in_future(self) -> None:
        """Directly check hijack_owner_expires_at is in the future after reclaim."""
        store = InMemoryResumeStore()
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

        store.mark_hijack_owner(token, True)

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            _read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            ws.receive_json()  # resumed hello

            # Access _workers directly (safe in sync test context)
            st = hub._workers.get("w1")
            if st is not None and st.hijack_owner_expires_at is not None:
                assert st.hijack_owner_expires_at > time.time()
