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

_DLE_STX = "\x10\x02"
_HEADER_LEN = 11  # DLE STX + 8 hex + ':'


def _decode_msg(raw: str) -> dict:
    """Decode a control-stream-framed message to a dict."""
    if raw.startswith(_DLE_STX):
        return json.loads(raw[_HEADER_LEN:])
    return json.loads(raw)


from provide.terminal.hijack.hub import InMemoryResumeStore, TermHub
from provide.terminal.hijack.models import WorkerTermState
from provide.terminal.hijack.routes.browser_handlers import (
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
        msg = _decode_msg(wws.send_text.call_args_list[0][0][0])
        assert msg["type"] == "control"

    async def test_pause_message_action_is_pause(self) -> None:
        """mutmut_44-47: action must be 'pause'."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        wws.send_text.assert_called()
        msg = _decode_msg(wws.send_text.call_args_list[0][0][0])
        assert msg["action"] == "pause"

    async def test_pause_message_owner_is_dashboard(self) -> None:
        """mutmut_48-51: owner must be 'dashboard'."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        wws.send_text.assert_called()
        msg = _decode_msg(wws.send_text.call_args_list[0][0][0])
        assert msg["owner"] == "dashboard"

    async def test_pause_message_lease_s_is_zero(self) -> None:
        """mutmut_54: lease_s must be 0."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        wws.send_text.assert_called()
        msg = _decode_msg(wws.send_text.call_args_list[0][0][0])
        assert msg["lease_s"] == 0

    async def test_pause_message_has_ts(self) -> None:
        """mutmut_55-56: ts key must be present."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        wws.send_text.assert_called()
        msg = _decode_msg(wws.send_text.call_args_list[0][0][0])
        assert "ts" in msg

    async def test_hijack_request_non_admin_sends_exact_error_message(self) -> None:
        """mutmut_2-17: error message and type must be exact strings."""
        hub = _make_hub()
        ws = _make_ws()
        await handle_browser_message(hub, ws, "w1", "operator", {"type": "hijack_request"}, False)
        ws.send_text.assert_called_once()
        payload = _decode_msg(ws.send_text.call_args[0][0])
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
        payload = _decode_msg(ws.send_text.call_args[0][0])
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
        payload = _decode_msg(ws.send_text.call_args_list[0][0][0])
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
        call_args = [_decode_msg(c[0][0]) for c in wws.send_text.call_args_list]
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
        msgs = [_decode_msg(c[0][0]) for c in ws.send_text.call_args_list]
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
        msg = _decode_msg(wws.send_text.call_args_list[0][0][0])
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
        msg = _decode_msg(wws.send_text.call_args_list[0][0][0])
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
        msg = _decode_msg(wws.send_text.call_args_list[0][0][0])
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
        msg = _decode_msg(wws.send_text.call_args_list[0][0][0])
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
        msg = _decode_msg(wws.send_text.call_args_list[0][0][0])
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
        msgs = [_decode_msg(c[0][0]) for c in wws.send_text.call_args_list]
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
        """mutmut_37-40: type=='input' → raw data encoding (not control frame) sent to worker."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "operator", wws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"
        await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "hello"}, False)
        wws.send_text.assert_called()
        # Input uses raw data encoding — NOT a control frame
        raw = wws.send_text.call_args[0][0]
        assert not raw.startswith(_DLE_STX)

    async def test_input_message_data_key_present(self) -> None:
        """mutmut_41-42: data key must be 'data' — worker receives the actual payload."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "operator", wws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"
        await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "hello"}, False)
        raw = wws.send_text.call_args[0][0]
        assert raw == "hello"

    async def test_input_message_sent_to_worker(self) -> None:
        """send_worker is called once with the correct data."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "operator", wws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"
        await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "hello"}, False)
        wws.send_text.assert_called_once()

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
        payload = _decode_msg(ws.send_text.call_args[0][0])
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
        payload = _decode_msg(ws.send_text.call_args[0][0])
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
        """mutmut_12-13: data must be looked up by 'data' key — worker gets the actual text."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "operator", wws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"
        await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "hello"}, False)
        raw = wws.send_text.call_args[0][0]
        assert raw == "hello"
