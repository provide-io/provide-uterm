#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Direct unit tests for browser_handlers.handle_browser_message.

These tests call handle_browser_message with a real TermHub and mock WebSocket
objects, isolating the handler logic without requiring a full ASGI server.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from provide.uterm.bridge.hub import TermHub
from provide.uterm.bridge.models import WorkerTermState
from provide.uterm.bridge.routes.browser_handlers import handle_browser_message

from tests.bridge.control_channel_helpers import decode_control_payload


def _make_hub() -> TermHub:
    return TermHub()


def _make_ws() -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


def _make_worker_ws() -> MagicMock:
    wws = MagicMock()
    wws.send_text = AsyncMock()
    return wws


async def _register(hub: TermHub, worker_id: str, browser_ws: Any, role: str, worker_ws: Any | None = None) -> None:
    async with hub._lock:
        st = hub._workers.setdefault(worker_id, WorkerTermState())
        st.browsers[browser_ws] = role
        if worker_ws is not None:
            st.worker_ws = worker_ws


class TestPingPong:
    async def test_ping_sends_pong_and_returns_owned_hijack_unchanged(self) -> None:
        hub = _make_hub()
        ws = _make_ws()
        result = await handle_browser_message(hub, ws, "w1", "admin", {"type": "ping"}, False)
        assert result is False
        ws.send_text.assert_called_once()
        sent = decode_control_payload(ws.send_text.call_args[0][0])
        assert sent["type"] == "pong"
        assert "ts" in sent

    async def test_ping_preserves_owned_hijack_true(self) -> None:
        hub = _make_hub()
        ws = _make_ws()
        result = await handle_browser_message(hub, ws, "w1", "admin", {"type": "ping"}, True)
        assert result is True
        ws.send_text.assert_called_once()
        sent = decode_control_payload(ws.send_text.call_args[0][0])
        assert sent["type"] == "pong"


class TestSnapshotReq:
    async def test_snapshot_req_forwarded_when_no_hijack(self) -> None:
        hub = _make_hub()
        ws = _make_ws()
        worker_ws = _make_worker_ws()
        await _register(hub, "w1", ws, "operator", worker_ws)
        await handle_browser_message(hub, ws, "w1", "operator", {"type": "snapshot_req"}, False)
        worker_ws.send_text.assert_called()

    async def test_snapshot_req_suppressed_when_hijack_active_and_not_owner(self) -> None:
        hub = _make_hub()
        ws = _make_ws()
        owner_ws = _make_ws()
        worker_ws = _make_worker_ws()
        await _register(hub, "w1", ws, "operator", worker_ws)
        async with hub._lock:
            st = hub._workers["w1"]
            st.browsers[owner_ws] = "admin"
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.monotonic() + 60
        worker_ws.send_text.reset_mock()
        await handle_browser_message(hub, ws, "w1", "operator", {"type": "snapshot_req"}, False)
        # Snapshot request should NOT be forwarded to the worker because hijack is active
        # and this browser is not the owner.
        worker_ws.send_text.assert_not_called()


class TestHeartbeat:
    async def test_heartbeat_ack_sent_when_owner(self) -> None:
        hub = _make_hub()
        ws = _make_ws()
        worker_ws = _make_worker_ws()
        await _register(hub, "w1", ws, "admin", worker_ws)
        async with hub._lock:
            st = hub._workers["w1"]
            st.hijack_owner = ws
            st.hijack_owner_expires_at = time.monotonic() + 60
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "heartbeat"}, True)
        # First call is heartbeat_ack; subsequent calls are hijack_state broadcasts.
        first_sent = ws.send_text.call_args_list[0][0][0]
        payload = decode_control_payload(first_sent)
        assert payload["type"] == "heartbeat_ack"
        assert "lease_expires_at" in payload

    async def test_heartbeat_no_ack_when_not_owner(self) -> None:
        hub = _make_hub()
        ws = _make_ws()
        await _register(hub, "w1", ws, "operator")
        await handle_browser_message(hub, ws, "w1", "operator", {"type": "heartbeat"}, False)
        ws.send_text.assert_not_called()


class TestHijackRequest:
    async def test_hijack_request_rejected_for_non_admin(self) -> None:
        hub = _make_hub()
        ws = _make_ws()
        worker_ws = _make_worker_ws()
        await _register(hub, "w1", ws, "operator", worker_ws)
        result = await handle_browser_message(hub, ws, "w1", "operator", {"type": "hijack_request"}, False)
        assert result is False
        sent = ws.send_text.call_args[0][0]
        assert "admin" in decode_control_payload(sent)["message"].lower()

    async def test_hijack_request_rejected_in_open_mode(self) -> None:
        hub = _make_hub()
        ws = _make_ws()
        worker_ws = _make_worker_ws()
        await _register(hub, "w1", ws, "admin", worker_ws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"
        result = await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        assert result is False
        sent = ws.send_text.call_args[0][0]
        assert "open" in decode_control_payload(sent)["message"].lower()

    async def test_hijack_request_fails_when_no_worker(self) -> None:
        hub = _make_hub()
        ws = _make_ws()
        await _register(hub, "w1", ws, "admin")  # no worker_ws
        result = await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        assert result is False

    async def test_hijack_request_acquires_and_returns_true(self) -> None:
        hub = _make_hub()
        ws = _make_ws()
        worker_ws = _make_worker_ws()
        await _register(hub, "w1", ws, "admin", worker_ws)
        result = await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        assert result is True
        async with hub._lock:
            st = hub._workers["w1"]
            assert st.hijack_owner is ws


class TestHijackRelease:
    async def test_hijack_release_clears_owner(self) -> None:
        hub = _make_hub()
        ws = _make_ws()
        worker_ws = _make_worker_ws()
        await _register(hub, "w1", ws, "admin", worker_ws)
        async with hub._lock:
            st = hub._workers["w1"]
            st.hijack_owner = ws
            st.hijack_owner_expires_at = time.monotonic() + 60
        result = await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_release"}, True)
        assert result is False
        async with hub._lock:
            st2 = hub._workers["w1"]
            assert st2.hijack_owner is None

    async def test_hijack_release_noop_when_not_owner(self) -> None:
        hub = _make_hub()
        ws = _make_ws()
        await _register(hub, "w1", ws, "admin")
        result = await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_release"}, False)
        assert result is False


class TestInput:
    async def test_input_forwarded_in_open_mode(self) -> None:
        hub = _make_hub()
        ws = _make_ws()
        worker_ws = _make_worker_ws()
        await _register(hub, "w1", ws, "operator", worker_ws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"
        await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "hi\r"}, False)
        worker_ws.send_text.assert_called()
        assert worker_ws.send_text.call_args[0][0] == "hi\r"

    async def test_input_rejected_for_viewer(self) -> None:
        hub = _make_hub()
        ws = _make_ws()
        worker_ws = _make_worker_ws()
        await _register(hub, "w1", ws, "viewer", worker_ws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"
        await handle_browser_message(hub, ws, "w1", "viewer", {"type": "input", "data": "hi\r"}, False)
        worker_ws.send_text.assert_not_called()

    async def test_input_too_long_sends_error(self) -> None:
        # max_input_chars is clamped to >= 100 in the constructor; use 200 chars to exceed it.
        hub = _make_hub()
        hub.max_input_chars = 10  # patch directly after construction
        ws = _make_ws()
        worker_ws = _make_worker_ws()
        await _register(hub, "w1", ws, "operator", worker_ws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"
        await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "x" * 20}, False)
        ws.send_text.assert_called()
        err = decode_control_payload(ws.send_text.call_args[0][0])
        assert err["type"] == "error"
        worker_ws.send_text.assert_not_called()

    async def test_input_empty_data_is_ignored(self) -> None:
        hub = _make_hub()
        ws = _make_ws()
        worker_ws = _make_worker_ws()
        await _register(hub, "w1", ws, "operator", worker_ws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"
        await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": ""}, False)
        worker_ws.send_text.assert_not_called()


class TestUnknownType:
    async def test_unknown_type_returns_unchanged_owned_hijack(self) -> None:
        hub = _make_hub()
        ws = _make_ws()
        result = await handle_browser_message(hub, ws, "w1", "admin", {"type": "unknown_future_type"}, True)
        assert result is True
        ws.send_text.assert_not_called()
