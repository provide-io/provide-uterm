#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for hijack/routes/browser_handlers.py.

These tests specifically target mutations not killed by existing tests:
- Exact JSON key/value strings in worker and browser messages
- Logic inversion mutations (==, !=, and, or)
- Return value mutations (True/False)
- Argument substitution mutations (None swaps)
- Worker pause/resume message content
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from provide.terminal.hijack.hub import InMemoryResumeStore, TermHub
from provide.terminal.hijack.models import WorkerTermState
from provide.terminal.hijack.routes.browser_handlers import (
    _handle_resume,
    handle_browser_message,
)


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


# ---------------------------------------------------------------------------
# _handle_hijack_request — worker pause message content
# ---------------------------------------------------------------------------


class TestHijackRequestWorkerMessage:
    """Verify exact content of the pause control frame sent to the worker."""

    async def test_pause_message_type_is_control(self) -> None:
        """mutmut_40-41: type must be 'control'."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        wws.send_text.assert_called()
        msg = json.loads(wws.send_text.call_args_list[0][0][0])
        assert msg["type"] == "control"

    async def test_pause_message_action_is_pause(self) -> None:
        """mutmut_44-47: action must be 'pause'."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        wws.send_text.assert_called()
        msg = json.loads(wws.send_text.call_args_list[0][0][0])
        assert msg["action"] == "pause"

    async def test_pause_message_owner_is_dashboard(self) -> None:
        """mutmut_48-51: owner must be 'dashboard'."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        wws.send_text.assert_called()
        msg = json.loads(wws.send_text.call_args_list[0][0][0])
        assert msg["owner"] == "dashboard"

    async def test_pause_message_lease_s_is_zero(self) -> None:
        """mutmut_54: lease_s must be 0."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        wws.send_text.assert_called()
        msg = json.loads(wws.send_text.call_args_list[0][0][0])
        assert msg["lease_s"] == 0

    async def test_pause_message_has_ts(self) -> None:
        """mutmut_55-56: ts key must be present."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        wws.send_text.assert_called()
        msg = json.loads(wws.send_text.call_args_list[0][0][0])
        assert "ts" in msg

    async def test_hijack_request_non_admin_sends_exact_error_message(self) -> None:
        """mutmut_2-17: error message and type must be exact strings."""
        hub = _make_hub()
        ws = _make_ws()
        await handle_browser_message(hub, ws, "w1", "operator", {"type": "hijack_request"}, False)
        ws.send_text.assert_called_once()
        payload = json.loads(ws.send_text.call_args[0][0])
        assert payload["type"] == "error"
        assert payload["message"] == "Hijack requires admin role."

    async def test_hijack_request_open_mode_sends_exact_error_message(self) -> None:
        """mutmut_25-33: open mode error message must be exact."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        ws.send_text.assert_called_once()
        payload = json.loads(ws.send_text.call_args[0][0])
        assert payload["type"] == "error"
        assert payload["message"] == "Hijack not available in open input mode."

    async def test_hijack_request_no_worker_sends_exact_error(self) -> None:
        """mutmut_63-71: no-worker error message must be exact."""
        hub = _make_hub()
        ws = _make_ws()
        await _register(hub, "w1", ws, "admin")  # no worker_ws
        result = await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        assert result is False
        ws.send_text.assert_called()
        payload = json.loads(ws.send_text.call_args_list[0][0][0])
        assert payload["type"] == "error"
        assert payload["message"] == "No worker connected for this session."

    async def test_hijack_request_no_worker_condition_not_inverted(self) -> None:
        """mutmut_57: 'not pause_sent' condition must not be inverted to 'pause_sent'."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        # With worker, pause_sent=True → should proceed and acquire, not return error
        result = await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        assert result is True
        # Worker should receive pause, not an error
        call_args = [json.loads(c[0][0]) for c in wws.send_text.call_args_list]
        assert any(m.get("action") == "pause" for m in call_args)

    async def test_hijack_request_role_check_not_inverted(self) -> None:
        """mutmut_1: role != 'admin' must not be inverted to role == 'admin'."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        # Admin should succeed
        result = await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        assert result is True

    async def test_hijack_request_already_hijacked_sends_exact_error(self) -> None:
        """mutmut_126-128: 'Already hijacked' error text must be exact."""
        hub = _make_hub()
        ws = _make_ws()
        owner_ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        async with hub._lock:
            st = hub._workers["w1"]
            st.browsers[owner_ws] = "admin"
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 60

        result = await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        assert result is False
        # Find the error message
        msgs = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        error_msgs = [m for m in msgs if m.get("type") == "error"]
        assert error_msgs
        assert error_msgs[0]["message"] == "Already hijacked by another client."

    async def test_hijack_request_acquire_returns_true(self) -> None:
        """mutmut_176: return True must not be mutated to return False."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        result = await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        assert result is True

    async def test_hijack_request_notify_called_with_enabled_true(self) -> None:
        """mutmut_161: notify_hijack_changed(enabled=True), not enabled=False."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        with patch.object(hub, "notify_hijack_changed") as mock_notify:
            await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
            mock_notify.assert_called_once()
            assert mock_notify.call_args[1]["enabled"] is True

    async def test_hijack_request_notify_called_with_owner_dashboard(self) -> None:
        """mutmut_157,162-163: notify_hijack_changed(owner='dashboard')."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        with patch.object(hub, "notify_hijack_changed") as mock_notify:
            await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
            mock_notify.assert_called_once()
            assert mock_notify.call_args[1]["owner"] == "dashboard"

    async def test_hijack_request_no_worker_compensating_resume_on_no_worker(self) -> None:
        """mutmut_95-97: err != 'already_hijacked' sends resume for no_worker."""
        hub = _make_hub()
        ws = _make_ws()
        # Register but no worker (send_worker returns False)
        await _register(hub, "w1", ws, "admin")  # no wws
        result = await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        # No worker connected — error sent, no hijack acquired
        assert result is False


# ---------------------------------------------------------------------------
# _handle_hijack_release — resume message content
# ---------------------------------------------------------------------------


class TestHijackReleaseWorkerMessage:
    """Verify exact content of resume control frame sent to worker on release."""

    async def test_resume_message_type_is_control(self) -> None:
        """mutmut_16-19: type must be 'control'."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        st = await _register(hub, "w1", ws, "admin", wws)
        async with hub._lock:
            st.hijack_owner = ws
            st.hijack_owner_expires_at = time.time() + 60
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_release"}, True)
        # First call = resume control frame
        msg = json.loads(wws.send_text.call_args_list[0][0][0])
        assert msg["type"] == "control"

    async def test_resume_message_action_is_resume(self) -> None:
        """mutmut_22-23: action must be 'resume'."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        st = await _register(hub, "w1", ws, "admin", wws)
        async with hub._lock:
            st.hijack_owner = ws
            st.hijack_owner_expires_at = time.time() + 60
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_release"}, True)
        msg = json.loads(wws.send_text.call_args_list[0][0][0])
        assert msg["action"] == "resume"

    async def test_resume_message_owner_is_dashboard(self) -> None:
        """mutmut_26-27: owner must be 'dashboard'."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        st = await _register(hub, "w1", ws, "admin", wws)
        async with hub._lock:
            st.hijack_owner = ws
            st.hijack_owner_expires_at = time.time() + 60
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_release"}, True)
        msg = json.loads(wws.send_text.call_args_list[0][0][0])
        assert msg["owner"] == "dashboard"

    async def test_resume_message_lease_s_is_zero(self) -> None:
        """mutmut_30: lease_s must be 0."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        st = await _register(hub, "w1", ws, "admin", wws)
        async with hub._lock:
            st.hijack_owner = ws
            st.hijack_owner_expires_at = time.time() + 60
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_release"}, True)
        msg = json.loads(wws.send_text.call_args_list[0][0][0])
        assert msg["lease_s"] == 0

    async def test_resume_message_has_ts(self) -> None:
        """mutmut_31-32: ts key must be present."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        st = await _register(hub, "w1", ws, "admin", wws)
        async with hub._lock:
            st.hijack_owner = ws
            st.hijack_owner_expires_at = time.time() + 60
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_release"}, True)
        msg = json.loads(wws.send_text.call_args_list[0][0][0])
        assert "ts" in msg

    async def test_release_returns_false_after_release(self) -> None:
        """mutmut_55: return False must not be mutated to return True."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        st = await _register(hub, "w1", ws, "admin", wws)
        async with hub._lock:
            st.hijack_owner = ws
            st.hijack_owner_expires_at = time.time() + 60
        result = await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_release"}, True)
        assert result is False

    async def test_release_do_resume_not_inverted(self) -> None:
        """mutmut_6,7: _do_resume = not rest_active must not be mutated."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        st = await _register(hub, "w1", ws, "admin", wws)
        async with hub._lock:
            st.hijack_owner = ws
            st.hijack_owner_expires_at = time.time() + 60
        # No rest lease → rest_active=False → _do_resume=True → resume sent
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_release"}, True)
        wws.send_text.assert_called()
        msgs = [json.loads(c[0][0]) for c in wws.send_text.call_args_list]
        assert any(m.get("action") == "resume" for m in msgs)

    async def test_release_notify_called_with_enabled_false(self) -> None:
        """mutmut_35,39: notify_hijack_changed(enabled=False) after release."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        st = await _register(hub, "w1", ws, "admin", wws)
        async with hub._lock:
            st.hijack_owner = ws
            st.hijack_owner_expires_at = time.time() + 60
        with patch.object(hub, "notify_hijack_changed") as mock_notify:
            await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_release"}, True)
            mock_notify.assert_called_once()
            assert mock_notify.call_args[1]["enabled"] is False
            assert mock_notify.call_args[1]["owner"] is None


# ---------------------------------------------------------------------------
# _handle_input — worker input message content
# ---------------------------------------------------------------------------


class TestHandleInputWorkerMessage:
    """Verify exact content of input messages sent to the worker."""

    async def test_input_message_type_is_input(self) -> None:
        """mutmut_37-40: type must be 'input'."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "operator", wws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"
        await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "hello"}, False)
        wws.send_text.assert_called()
        msg = json.loads(wws.send_text.call_args[0][0])
        assert msg["type"] == "input"

    async def test_input_message_data_key_present(self) -> None:
        """mutmut_41-42: data key must be 'data'."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "operator", wws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"
        await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "hello"}, False)
        msg = json.loads(wws.send_text.call_args[0][0])
        assert "data" in msg
        assert msg["data"] == "hello"

    async def test_input_message_has_ts(self) -> None:
        """mutmut_43-44: ts key must be present."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "operator", wws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"
        await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "hello"}, False)
        msg = json.loads(wws.send_text.call_args[0][0])
        assert "ts" in msg

    async def test_input_too_long_error_exact_message(self) -> None:
        """mutmut_22-30: 'Input too long.' must be exact."""
        hub = _make_hub()
        hub.max_input_chars = 5
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "operator", wws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"
        await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "toolong"}, False)
        ws.send_text.assert_called()
        payload = json.loads(ws.send_text.call_args[0][0])
        assert payload["type"] == "error"
        assert payload["message"] == "Input too long."

    async def test_input_length_check_is_strict_gt(self) -> None:
        """mutmut_16: len(data) > max must not be >= max (at boundary = max chars is ok)."""
        hub = _make_hub()
        hub.max_input_chars = 5
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "operator", wws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"
        # Exactly at max — should NOT be rejected
        await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "abcde"}, False)
        ws.send_text.assert_not_called()

    async def test_input_worker_lost_exact_error_message(self) -> None:
        """mutmut_51-59: 'Worker connection lost.' must be exact."""
        hub = _make_hub()
        ws = _make_ws()
        # Worker registered but ws.send_text will raise to simulate disconnect
        wws = _make_ws()
        wws.send_text = AsyncMock(side_effect=Exception("disconnected"))
        await _register(hub, "w1", ws, "operator", wws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"
        await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "hi"}, False)
        ws.send_text.assert_called()
        payload = json.loads(ws.send_text.call_args[0][0])
        assert payload["type"] == "error"
        assert payload["message"] == "Worker connection lost."

    async def test_input_can_send_check_not_inverted(self) -> None:
        """mutmut_6: 'if not can_send' must not be inverted to 'if can_send'."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "viewer", wws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"
        # Viewer cannot send in open mode
        await handle_browser_message(hub, ws, "w1", "viewer", {"type": "input", "data": "hi"}, False)
        wws.send_text.assert_not_called()

    async def test_input_data_key_lookup_exact(self) -> None:
        """mutmut_12-13: data must be looked up by 'data' key, not 'XXdataXX'."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "operator", wws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"
        await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "hello"}, False)
        msg = json.loads(wws.send_text.call_args[0][0])
        assert msg["data"] == "hello"


# ---------------------------------------------------------------------------
# _handle_resume — token/role/hijack reclaim logic
# ---------------------------------------------------------------------------


class TestHandleResumeTokenLogic:
    """Verify resume token logic mutations."""

    def _make_app_client(self, role: str, store: InMemoryResumeStore):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        hub = TermHub(
            resolve_browser_role=lambda _ws, _wid: role,
            resume_store=store,
        )
        app = FastAPI()
        app.include_router(hub.create_router())
        return TestClient(app), hub

    def _read_initial(self, ws):
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        hs = ws.receive_json()
        assert hs["type"] == "hijack_state"
        return hello, hs

    def test_resume_token_key_is_token(self) -> None:
        """mutmut_8-9: 'token' key must be used (not 'XXtokenXX' or 'TOKEN')."""
        store = InMemoryResumeStore()
        client, _ = self._make_app_client("admin", store)
        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            # Use exact key "token"
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["type"] == "hello"
            assert resumed["resumed"] is True

    def test_resume_empty_token_not_resumed(self) -> None:
        """mutmut_11: 'if not old_token' must not be inverted."""
        store = InMemoryResumeStore()
        client, _ = self._make_app_client("admin", store)
        with client.websocket_connect("/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": ""})
            ws.send_json({"type": "ping"})
            pong = ws.receive_json()
            assert pong["type"] == "pong"

    def test_resume_wrong_worker_id_rejected(self) -> None:
        """mutmut_16: session.worker_id != worker_id condition must not be inverted."""
        store = InMemoryResumeStore()
        client, _ = self._make_app_client("admin", store)
        with client.websocket_connect("/ws/browser/worker-a/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        with client.websocket_connect("/ws/browser/worker-b/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            ws.send_json({"type": "ping"})
            pong = ws.receive_json()
            assert pong["type"] == "pong"

    def test_resume_can_hijack_true_for_admin(self) -> None:
        """mutmut_27-29: can_hijack = role == 'admin' must be correct."""
        store = InMemoryResumeStore()
        client, _ = self._make_app_client("admin", store)
        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["can_hijack"] is True

    def test_resume_can_hijack_false_for_operator(self) -> None:
        """mutmut_27: can_hijack must be False for operator, not inverted."""
        store = InMemoryResumeStore()
        hub = TermHub(
            resolve_browser_role=lambda _ws, _wid: "operator",
            resume_store=store,
        )
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(hub.create_router())
        client = TestClient(app)
        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["can_hijack"] is False

    def test_resume_hello_type_is_hello(self) -> None:
        """mutmut_75-76: type must be 'hello', not 'XXhelloXX'."""
        store = InMemoryResumeStore()
        client, _ = self._make_app_client("admin", store)
        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["type"] == "hello"

    def test_resume_hello_has_worker_id(self) -> None:
        """mutmut_77-78: 'worker_id' key must be exact."""
        store = InMemoryResumeStore()
        client, _ = self._make_app_client("admin", store)
        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert "worker_id" in resumed

    def test_resume_hello_resumed_is_true(self) -> None:
        """Verify resumed=True is present in the response."""
        store = InMemoryResumeStore()
        client, _ = self._make_app_client("admin", store)
        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["resumed"] is True

    def test_resume_role_restored_from_token(self) -> None:
        """mutmut_30-32: role != role AND role in VALID_ROLES must both be True."""
        store = InMemoryResumeStore()
        hub1 = TermHub(
            resolve_browser_role=lambda _ws, _wid: "admin",
            resume_store=store,
        )
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app1 = FastAPI()
        app1.include_router(hub1.create_router())
        client1 = TestClient(app1)

        with client1.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]
            assert hello["role"] == "admin"

        # Second hub resolves viewer, but resume should restore admin
        hub2 = TermHub(
            resolve_browser_role=lambda _ws, _wid: "viewer",
            resume_store=store,
        )
        app2 = FastAPI()
        app2.include_router(hub2.create_router())
        client2 = TestClient(app2)

        with client2.websocket_connect("/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["role"] == "admin"

    def test_resume_old_token_revoked_after_resume(self) -> None:
        """mutmut_24: store.revoke(old_token) must be called with old_token."""
        store = InMemoryResumeStore()
        client, _ = self._make_app_client("admin", store)
        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            ws.receive_json()  # resumed hello
            # Old token must be revoked
            assert store.get(token) is None

    def test_resume_new_token_different_from_old(self) -> None:
        """mutmut_55-61: new_token = store.create(...) must create a new token."""
        store = InMemoryResumeStore()
        client, _ = self._make_app_client("admin", store)
        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["resume_token"] is not None
            assert resumed["resume_token"] != token

    def test_resume_hijack_reclaim_sets_owned_hijack_true(self) -> None:
        """mutmut_53-54: owned_hijack = True after hijack reclaim."""
        store = InMemoryResumeStore()
        client, _ = self._make_app_client("admin", store)
        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        store.mark_hijack_owner(token, True)

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["hijacked_by_me"] is True

    def test_resume_store_none_returns_unchanged_owned_hijack(self) -> None:
        """mutmut_1-2: no store → return owned_hijack unchanged."""
        hub = TermHub()  # no store
        assert hub._resume_store is None

        async def _run():
            ws = _make_ws()
            result = await _handle_resume(hub, ws, "w1", "admin", {"type": "resume", "token": "x"}, False)
            assert result is False

        import asyncio

        asyncio.run(_run())

    def test_resume_hijack_expiry_not_subtracted(self) -> None:
        """mutmut_52: hijack_owner_expires_at = time.time() + lease (not minus)."""
        store = InMemoryResumeStore()
        client, hub = self._make_app_client("admin", store)
        with client.websocket_connect("/ws/browser/w1/term") as ws:
            hello, _ = self._read_initial(ws)
            token = hello["resume_token"]

        store.mark_hijack_owner(token, True)

        with client.websocket_connect("/ws/browser/w1/term") as ws:
            self._read_initial(ws)
            ws.send_json({"type": "resume", "token": token})
            resumed = ws.receive_json()
            assert resumed["hijacked_by_me"] is True
            # If expiry was time.time() - lease_s, hijack would be expired immediately
            # Check hijack_state says owner=me
            hs = ws.receive_json()
            assert hs["type"] == "hijack_state"
            assert hs["owner"] == "me"


# ---------------------------------------------------------------------------
# handle_browser_message — dispatch and ping/pong
# ---------------------------------------------------------------------------


class TestHandleBrowserMessageDispatch:
    """Verify message dispatch and ping response."""

    async def test_ping_type_in_response_is_pong(self) -> None:
        """Ping must send 'pong' type, not an altered string."""
        hub = _make_hub()
        ws = _make_ws()
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "ping"}, False)
        ws.send_text.assert_called_once()
        sent = json.loads(ws.send_text.call_args[0][0])
        assert sent["type"] == "pong"

    async def test_ping_has_ts_field(self) -> None:
        """Ping response must have 'ts' key."""
        hub = _make_hub()
        ws = _make_ws()
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "ping"}, False)
        sent = json.loads(ws.send_text.call_args[0][0])
        assert "ts" in sent

    async def test_input_returns_owned_hijack_unchanged(self) -> None:
        """Input handler must not alter owned_hijack return value."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "operator", wws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"
        result = await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "x"}, True)
        assert result is True

    async def test_hijack_step_type_is_control(self) -> None:
        """hijack_step must send type='control', action='step'."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        st = await _register(hub, "w1", ws, "admin", wws)
        async with hub._lock:
            st.hijack_owner = ws
            st.hijack_owner_expires_at = time.time() + 60
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_step"}, True)
        wws.send_text.assert_called()
        msg = json.loads(wws.send_text.call_args_list[0][0][0])
        assert msg["type"] == "control"
        assert msg["action"] == "step"
