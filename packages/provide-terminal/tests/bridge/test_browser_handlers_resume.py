#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Resume token and dispatch tests for hijack/routes/browser_handlers.py."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

_DLE_STX = "\x10\x02"
_HEADER_LEN = 11  # DLE STX + 8 hex + ':'


def _decode_msg(raw: str) -> dict:
    """Decode a control-channel-framed message to a dict."""
    if raw.startswith(_DLE_STX):
        return json.loads(raw[_HEADER_LEN:])
    return json.loads(raw)


from fastapi import FastAPI
from fastapi.testclient import TestClient
from provide.terminal.bridge.hub import InMemoryResumeStore, TermHub
from provide.terminal.bridge.models import WorkerTermState
from provide.terminal.bridge.routes.browser_handlers import (
    _handle_resume,
    handle_browser_message,
)
from provide.terminal.client import connect_test_ws


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


class TestHandleResumeTokenLogic:
    """Verify resume token logic mutations."""

    def _make_app_client(self, role: str, store: InMemoryResumeStore):
        hub = TermHub(
            resolve_browser_role=lambda _ws, _wid: role,
            resume_store=store,
        )
        app = FastAPI()
        app.include_router(hub.create_router())
        return TestClient(app), hub

    def _drain_until(self, ws, type_, timeout_s=1.0):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            msg = ws.receive_json()
            if msg["type"] == type_:
                return msg
            # Discard snapshots
            if msg["type"] == "snapshot":
                continue
            raise AssertionError(f"Expected {type_}, got {msg['type']}")
        raise TimeoutError(f"Timed out waiting for {type_}")

    def _read_initial(self, ws):
        hello = self._drain_until(ws, "hello")
        hs = self._drain_until(ws, "hijack_state")
        return hello, hs

    def test_resume_token_key_is_token(self) -> None:
        """mutmut_8-9: 'token' key must be used."""
        store = InMemoryResumeStore()
        client, _ = self._make_app_client("admin", store)
        with connect_test_ws(client, "/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        with connect_test_ws(client, "/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["type"] == "hello"
            assert resumed["resumed"] is True

    def test_resume_empty_token_not_resumed(self) -> None:
        """mutmut_11: 'if not old_token' must not be inverted."""
        store = InMemoryResumeStore()
        client, _ = self._make_app_client("admin", store)
        with connect_test_ws(client, "/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": ""})
            ws.send_json({"type": "ping"})
            pong = ws.receive_json()
            assert pong["type"] == "pong"

    def test_resume_wrong_worker_id_rejected(self) -> None:
        """mutmut_16: session.worker_id != worker_id condition must not be inverted."""
        store = InMemoryResumeStore()
        client, _ = self._make_app_client("admin", store)
        with connect_test_ws(client, "/ws/browser/worker-a/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        with connect_test_ws(client, "/ws/browser/worker-b/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            ws.send_json({"type": "ping"})
            pong = ws.receive_json()
            assert pong["type"] == "pong"

    def test_resume_can_hijack_true_for_admin(self) -> None:
        """mutmut_27-29: can_hijack = role == 'admin' must be correct."""
        store = InMemoryResumeStore()
        client, _ = self._make_app_client("admin", store)
        with connect_test_ws(client, "/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        with connect_test_ws(client, "/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["can_hijack"] is True

    def test_resume_can_hijack_false_for_operator(self) -> None:
        """mutmut_27: can_hijack must be False for operator."""
        store = InMemoryResumeStore()
        hub = TermHub(resolve_browser_role=lambda _ws, _wid: "operator", resume_store=store)
        app = FastAPI()
        app.include_router(hub.create_router())
        client = TestClient(app)
        with connect_test_ws(client, "/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        with connect_test_ws(client, "/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["can_hijack"] is False

    def test_resume_hello_type_is_hello(self) -> None:
        """mutmut_75-76: type must be 'hello'."""
        store = InMemoryResumeStore()
        client, _ = self._make_app_client("admin", store)
        with connect_test_ws(client, "/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        with connect_test_ws(client, "/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["type"] == "hello"

    def test_resume_hello_has_worker_id(self) -> None:
        """mutmut_77-78: 'worker_id' key must be exact."""
        store = InMemoryResumeStore()
        client, _ = self._make_app_client("admin", store)
        with connect_test_ws(client, "/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        with connect_test_ws(client, "/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert "worker_id" in resumed

    def test_resume_hello_resumed_is_true(self) -> None:
        """Verify resumed=True is present in the response."""
        store = InMemoryResumeStore()
        client, _ = self._make_app_client("admin", store)
        with connect_test_ws(client, "/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        with connect_test_ws(client, "/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["resumed"] is True

    def test_resume_role_restored_from_token(self) -> None:
        """mutmut_30-32: role priority check must gate the restore."""
        store = InMemoryResumeStore()
        hub1 = TermHub(resolve_browser_role=lambda _ws, _wid: "viewer", resume_store=store)
        app1 = FastAPI()
        app1.include_router(hub1.create_router())
        client1 = TestClient(app1)

        with connect_test_ws(client1, "/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]
            assert hello["role"] == "viewer"

        hub2 = TermHub(resolve_browser_role=lambda _ws, _wid: "admin", resume_store=store)
        app2 = FastAPI()
        app2.include_router(hub2.create_router())
        client2 = TestClient(app2)

        with connect_test_ws(client2, "/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["role"] == "viewer"

    def test_resume_old_token_revoked_after_resume(self) -> None:
        """mutmut_24: asyncio.run(store.revoke()old_token) must be called."""
        store = InMemoryResumeStore()
        client, _ = self._make_app_client("admin", store)
        with connect_test_ws(client, "/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        with connect_test_ws(client, "/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            ws.receive_json()
            assert asyncio.run(store.get(token)) is None

    def test_resume_new_token_different_from_old(self) -> None:
        """mutmut_55-61: new_token = asyncio.run(store.create(...)) must create a new token."""
        store = InMemoryResumeStore()
        client, _ = self._make_app_client("admin", store)
        with connect_test_ws(client, "/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        with connect_test_ws(client, "/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["resume_token"] is not None
            assert resumed["resume_token"] != token

    def test_resume_hijack_reclaim_sets_owned_hijack_true(self) -> None:
        """mutmut_53-54: owned_hijack = True after hijack reclaim."""
        store = InMemoryResumeStore()
        client, _ = self._make_app_client("admin", store)

        with connect_test_ws(client, "/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        asyncio.run(store.mark_hijack_owner(token, True))

        with connect_test_ws(client, "/ws/worker/w1/term") as worker:
            msg = worker.receive_json()
            assert msg["type"] == "snapshot_req"
            worker.send_json({"type": "snapshot", "screen": "", "ts": 1.0})

            with connect_test_ws(client, "/ws/browser/w1/term") as ws:
                self._read_initial(ws)
                ws.send_json({"type": "resume", "token": token})
                worker.receive_json()  # pause
                resumed = self._drain_until(ws, "hello")
                assert resumed["hijacked_by_me"] is True

    def test_resume_store_none_returns_unchanged_owned_hijack(self) -> None:
        """mutmut_1-2: no store → return owned_hijack unchanged."""
        hub = TermHub()
        assert hub._resume_store is None

        async def _run():
            ws = _make_ws()
            result = await _handle_resume(hub, ws, "w1", "admin", {"type": "resume", "token": "x"}, False)
            assert result is False

        asyncio.run(_run())

    def test_resume_hijack_expiry_not_subtracted(self) -> None:
        """mutmut_52: hijack_owner_expires_at = time.monotonic() + lease (not minus)."""
        store = InMemoryResumeStore()
        client, hub = self._make_app_client("admin", store)

        with connect_test_ws(client, "/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        asyncio.run(store.mark_hijack_owner(token, True))

        with connect_test_ws(client, "/ws/worker/w1/term") as worker:
            msg = worker.receive_json()
            assert msg["type"] == "snapshot_req"
            worker.send_json({"type": "snapshot", "screen": "", "ts": 1.0})

            with connect_test_ws(client, "/ws/browser/w1/term") as ws:
                self._read_initial(ws)
                ws.send_json({"type": "resume", "token": token})

                worker.receive_json()  # pause
                resumed = self._drain_until(ws, "hello")
                assert resumed["hijacked_by_me"] is True
                hs = self._drain_until(ws, "hijack_state")
                assert hs["owner"] == "me"


class TestHandleBrowserMessageDispatch:
    """Verify message dispatch and ping response."""

    async def test_ping_type_in_response_is_pong(self) -> None:
        hub = _make_hub()
        ws = _make_ws()
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "ping"}, False)
        ws.send_text.assert_called_once()
        sent = _decode_msg(ws.send_text.call_args[0][0])
        assert sent["type"] == "pong"

    async def test_ping_has_ts_field(self) -> None:
        hub = _make_hub()
        ws = _make_ws()
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "ping"}, False)
        sent = _decode_msg(ws.send_text.call_args[0][0])
        assert "ts" in sent

    async def test_input_returns_owned_hijack_unchanged(self) -> None:
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "operator", wws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"
        result = await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "x"}, True)
        assert result is True

    async def test_hijack_step_type_is_control(self) -> None:
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        st = await _register(hub, "w1", ws, "admin", wws)
        async with hub._lock:
            st.hijack_owner = ws
            st.hijack_owner_expires_at = time.monotonic() + 60
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_step"}, True)
        wws.send_text.assert_called()
        msg = _decode_msg(wws.send_text.call_args_list[0][0][0])
        assert msg["type"] == "control"
        assert msg["action"] == "step"


class TestHandleResumeBranchCoverage:
    """Cover arcs in _handle_resume that require was_hijack_owner=True."""

    async def test_resume_hijack_owner_no_worker_pause_not_sent(self) -> None:
        """Arc 263->284: was_hijack_owner=True, can_hijack=True, but no worker → pause_sent=False."""
        store = InMemoryResumeStore()
        hub = _make_hub(resume_store=store)
        ws = _make_ws()
        await _register(hub, "w1", ws, "admin")
        token = await store.create("w1", "admin", 300)
        await store.mark_hijack_owner(token, True)

        result = await _handle_resume(hub, ws, "w1", "admin", {"token": token}, False)
        assert result is False
        ws.send_text.assert_called()

    async def test_resume_hijack_owner_pause_sent_but_already_hijacked(self) -> None:
        """Arc 266->277: was_hijack_owner=True, pause_sent=True, inner condition False (owner set)."""
        store = InMemoryResumeStore()
        hub = _make_hub(resume_store=store)
        ws = _make_ws()
        other_ws = _make_ws()
        wws = _make_ws()
        st = await _register(hub, "w1", ws, "admin", wws)
        async with hub._lock:
            st.hijack_owner = other_ws
            st.hijack_owner_expires_at = time.monotonic() + 60
        token = await store.create("w1", "admin", 300)
        await store.mark_hijack_owner(token, True)

        result = await _handle_resume(hub, ws, "w1", "admin", {"token": token}, False)
        assert result is False
        ws.send_text.assert_called()
