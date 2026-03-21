#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for hijack/routes/browser_handlers.py.

Targets the surviving mutants not already killed by test_browser_handlers_mutations.py:
- heartbeat_ack message field keys (ts, lease_expires_at) and broadcast_hijack_state call
- hijack_step message field keys (owner, lease_s, ts) and append_event arguments
- _handle_input append_event event name and dict key/value arguments
- _handle_hijack_request err == "already_hijacked" vs != (conflict metric, compensating resume)
- _handle_hijack_request compensating-resume message content
- _handle_resume token default "", not truthy placeholder
- _handle_resume _on_resume callback invoked with correct (token, session) args
- _handle_resume new_role initialized from role (not None)
- _handle_resume can_hijack = role == "admin" (not !=)
- _handle_resume role-restore condition: AND not OR
- _handle_resume hijack-reclaim condition: is None AND not hub.is_hijacked()
- _handle_resume owned_hijack set to True (not False/None) on reclaim
- _handle_resume store.create called with correct (worker_id, new_role) positional args
- _handle_resume _ws_to_resume_token set to new_token (not None)
- _handle_resume hello message: hijacked key, worker_online key, input_mode key
- _handle_resume hello: hijack_step_supported True (not False), resume_supported True (not False)
- _handle_resume hello: capabilities sub-dict keys correct
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.terminal.hijack.hub import InMemoryResumeStore, TermHub
from provide.terminal.hijack.models import WorkerTermState
from provide.terminal.hijack.routes.browser_handlers import (
    handle_browser_message,
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


class TestHeartbeatMessage:
    """mutmut 38,40,47,48,49: heartbeat_ack message field keys/values."""

    async def test_heartbeat_ack_has_ts_key(self) -> None:
        """mutmut_47,48: 'ts' key must be present (not 'XXtsXX' or 'TS')."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        st = await _register(hub, "w1", ws, "admin", wws)
        # Set ownership so touch_if_owner returns non-None
        async with hub._lock:
            st.hijack_owner = ws
            st.hijack_owner_expires_at = time.time() + 60
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "heartbeat"}, True)
        ws.send_text.assert_called()
        sent = json.loads(ws.send_text.call_args_list[0][0][0])
        assert sent["type"] == "heartbeat_ack"
        assert "ts" in sent
        assert "lease_expires_at" in sent

    async def test_heartbeat_ack_broadcasts_state(self) -> None:
        """heartbeat must call broadcast_hijack_state so all browsers see updated lease."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        st = await _register(hub, "w1", ws, "admin", wws)
        async with hub._lock:
            st.hijack_owner = ws
            st.hijack_owner_expires_at = time.time() + 60
        with patch.object(hub, "broadcast_hijack_state", new_callable=AsyncMock) as mock_bcast:
            await handle_browser_message(hub, ws, "w1", "admin", {"type": "heartbeat"}, True)
            mock_bcast.assert_called_once_with("w1")

    async def test_heartbeat_no_owner_no_ack(self) -> None:
        """When touch_if_owner returns None (not owner), no heartbeat_ack is sent."""
        hub = _make_hub()
        ws = _make_ws()
        # No ownership established
        await _register(hub, "w1", ws, "admin")
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "heartbeat"}, False)
        # Should NOT have sent heartbeat_ack
        if ws.send_text.called:
            calls = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
            assert not any(m.get("type") == "heartbeat_ack" for m in calls)


# ---------------------------------------------------------------------------
# hijack_step message — field keys must be exact
# ---------------------------------------------------------------------------


class TestHijackStepMessage:
    """mutmut 85-93, 110-124: hijack_step worker message and post-step events."""

    async def _setup_owner(self, hub: TermHub, ws: Any, wws: Any) -> WorkerTermState:
        st = await _register(hub, "w1", ws, "admin", wws)
        async with hub._lock:
            st.hijack_owner = ws
            st.hijack_owner_expires_at = time.time() + 60
        return st

    async def test_step_message_owner_key_is_owner(self) -> None:
        """mutmut_85,86: 'owner' key must be 'owner' (not 'XXownerXX' or 'OWNER')."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await self._setup_owner(hub, ws, wws)
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_step"}, True)
        msg = json.loads(wws.send_text.call_args_list[0][0][0])
        assert "owner" in msg
        assert msg["owner"] == "dashboard"

    async def test_step_message_owner_value_is_dashboard(self) -> None:
        """mutmut_87,88: 'owner' value must be 'dashboard' (not 'XXdashboardXX' or 'DASHBOARD')."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await self._setup_owner(hub, ws, wws)
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_step"}, True)
        msg = json.loads(wws.send_text.call_args_list[0][0][0])
        assert msg["owner"] == "dashboard"

    async def test_step_message_lease_s_key_is_lease_s(self) -> None:
        """mutmut_89,90: 'lease_s' key must be 'lease_s' (not 'XXlease_sXX' or 'LEASE_S')."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await self._setup_owner(hub, ws, wws)
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_step"}, True)
        msg = json.loads(wws.send_text.call_args_list[0][0][0])
        assert "lease_s" in msg

    async def test_step_message_lease_s_value_is_zero(self) -> None:
        """mutmut_91: 'lease_s' value must be 0 (not 1)."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await self._setup_owner(hub, ws, wws)
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_step"}, True)
        msg = json.loads(wws.send_text.call_args_list[0][0][0])
        assert msg["lease_s"] == 0

    async def test_step_message_ts_key_is_ts(self) -> None:
        """mutmut_92,93: 'ts' key must be 'ts' (not 'XXtsXX' or 'TS')."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await self._setup_owner(hub, ws, wws)
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_step"}, True)
        msg = json.loads(wws.send_text.call_args_list[0][0][0])
        assert "ts" in msg

    async def test_step_metric_name_is_hijack_steps_total(self) -> None:
        """mutmut_110-112: metric name must be 'hijack_steps_total'."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await self._setup_owner(hub, ws, wws)
        with patch.object(hub, "metric") as mock_metric:
            await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_step"}, True)
            mock_metric.assert_called_once_with("hijack_steps_total")

    async def test_step_append_event_called_with_correct_args(self) -> None:
        """mutmut_113-124: append_event must be called with (worker_id, 'hijack_step', {'owner': 'dashboard_ws'})."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await self._setup_owner(hub, ws, wws)
        with patch.object(hub, "append_event", new_callable=AsyncMock) as mock_ae:
            await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_step"}, True)
            mock_ae.assert_called_once()
            call_args = mock_ae.call_args
            assert call_args[0][0] == "w1"  # worker_id
            assert call_args[0][1] == "hijack_step"  # event name
            assert call_args[0][2] == {"owner": "dashboard_ws"}  # event data

    async def test_step_no_worker_sends_error(self) -> None:
        """hijack_step with no worker sends error (not metric/append_event path)."""
        hub = _make_hub()
        ws = _make_ws()
        # Owner set but no worker_ws
        st = await _register(hub, "w1", ws, "admin")
        async with hub._lock:
            st.hijack_owner = ws
            st.hijack_owner_expires_at = time.time() + 60
        await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_step"}, True)
        ws.send_text.assert_called()
        msgs = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        assert any(m.get("type") == "error" for m in msgs)


# ---------------------------------------------------------------------------
# _handle_input — append_event arguments
# ---------------------------------------------------------------------------


class TestHandleInputAppendEvent:
    """mutmut_67-75: append_event called with correct event name and args."""

    async def _setup_open_mode(self, hub: TermHub, ws: Any, wws: Any) -> None:
        await _register(hub, "w1", ws, "operator", wws)
        async with hub._lock:
            hub._workers["w1"].input_mode = "open"

    async def test_append_event_event_name_is_input_send(self) -> None:
        """mutmut_67,68: event name must be 'input_send' (not 'XXinput_sendXX' or 'INPUT_SEND')."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await self._setup_open_mode(hub, ws, wws)
        with patch.object(hub, "append_event", new_callable=AsyncMock) as mock_ae:
            await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "hi"}, False)
            mock_ae.assert_called_once()
            assert mock_ae.call_args[0][1] == "input_send"

    async def test_append_event_owner_key_is_owner(self) -> None:
        """mutmut_69,70: 'owner' key must be 'owner' (not 'XXownerXX' or 'OWNER')."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await self._setup_open_mode(hub, ws, wws)
        with patch.object(hub, "append_event", new_callable=AsyncMock) as mock_ae:
            await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "hi"}, False)
            event_data = mock_ae.call_args[0][2]
            assert "owner" in event_data

    async def test_append_event_owner_value_is_dashboard_ws(self) -> None:
        """mutmut_71,72: 'owner' value must be 'dashboard_ws' (not 'XXdashboard_wsXX' or 'DASHBOARD_WS')."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await self._setup_open_mode(hub, ws, wws)
        with patch.object(hub, "append_event", new_callable=AsyncMock) as mock_ae:
            await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "hi"}, False)
            event_data = mock_ae.call_args[0][2]
            assert event_data["owner"] == "dashboard_ws"

    async def test_append_event_keys_key_is_keys(self) -> None:
        """mutmut_73,74: 'keys' key must be 'keys' (not 'XXkeysXX' or 'KEYS')."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await self._setup_open_mode(hub, ws, wws)
        with patch.object(hub, "append_event", new_callable=AsyncMock) as mock_ae:
            await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "hi"}, False)
            event_data = mock_ae.call_args[0][2]
            assert "keys" in event_data

    async def test_append_event_keys_truncated_at_120(self) -> None:
        """mutmut_75: data[:120] not data[:121] — keys are truncated at 120 chars."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await self._setup_open_mode(hub, ws, wws)
        long_data = "x" * 130  # longer than 120
        with patch.object(hub, "append_event", new_callable=AsyncMock) as mock_ae:
            await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": long_data}, False)
            event_data = mock_ae.call_args[0][2]
            # Truncated at 120, not 121 or beyond
            assert len(event_data["keys"]) == 120
            assert event_data["keys"] == "x" * 120

    async def test_append_event_worker_id_is_correct(self) -> None:
        """append_event first arg must be the actual worker_id."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await self._setup_open_mode(hub, ws, wws)
        with patch.object(hub, "append_event", new_callable=AsyncMock) as mock_ae:
            await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": "hi"}, False)
            assert mock_ae.call_args[0][0] == "w1"


# ---------------------------------------------------------------------------
# _handle_hijack_request — err == "already_hijacked" branching
# ---------------------------------------------------------------------------
