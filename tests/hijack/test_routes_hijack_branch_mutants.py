#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for hijack routes — already-hijacked branches and input handling."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.models import WorkerTermState
from provide.terminal.hijack.routes.browser_handlers import handle_browser_message

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_hub() -> TermHub:
    return TermHub()


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


class TestHijackRequestAlreadyHijackedBranch:
    """mutmut_89-94: err == 'already_hijacked' condition and metric must be correct."""

    async def test_conflict_metric_called_on_already_hijacked(self) -> None:
        """mutmut_89-94: metric('hijack_conflicts_total') called when err == 'already_hijacked'."""
        hub = _make_hub()
        ws = _make_ws()
        owner_ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        # Pre-set a different owner so try_acquire_ws_hijack returns (False, already_hijacked)
        async with hub._lock:
            st = hub._workers["w1"]
            st.browsers[owner_ws] = "admin"
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 60

        with patch.object(hub, "metric") as mock_metric:
            await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
            mock_metric.assert_called_once_with("hijack_conflicts_total")

    async def test_no_conflict_metric_on_no_worker(self) -> None:
        """mutmut_89: with err == 'no_worker', hijack_conflicts_total must NOT be called."""
        hub = _make_hub()
        ws = _make_ws()
        # No worker_ws registered → send_worker returns False → err == 'no_worker'
        await _register(hub, "w1", ws, "admin")

        with patch.object(hub, "metric") as mock_metric:
            await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
            # metric should not be called for hijack_conflicts_total
            for call in mock_metric.call_args_list:
                assert call[0][0] != "hijack_conflicts_total"

    async def test_compensating_resume_sent_on_no_worker(self) -> None:
        """mutmut_96,97: err != 'already_hijacked' → compensating resume IS sent for no_worker."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        # Force try_acquire_ws_hijack to return (False, "no_worker"). We patch directly.
        with (
            patch.object(hub, "try_acquire_ws_hijack", new_callable=AsyncMock, return_value=(False, "no_worker")),
            patch.object(hub, "send_worker", new_callable=AsyncMock) as mock_send,
        ):
            mock_send.return_value = True
            await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
            # send_worker should be called at least once for resume
            actions_sent = []
            for c in mock_send.call_args_list:
                msg = c[0][1] if len(c[0]) > 1 else c[1].get("msg", {})
                if isinstance(msg, dict):
                    actions_sent.append(msg.get("action"))
            assert "resume" in actions_sent, f"Compensating resume not sent. Actions: {actions_sent}"

    async def test_compensating_resume_not_sent_on_already_hijacked(self) -> None:
        """mutmut_96,97: err == 'already_hijacked' → compensating resume must NOT be sent."""
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

        with (
            patch.object(
                hub, "try_acquire_ws_hijack", new_callable=AsyncMock, return_value=(False, "already_hijacked")
            ),
            patch.object(hub, "send_worker", new_callable=AsyncMock) as mock_send,
        ):
            mock_send.return_value = True
            await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
            actions_sent = []
            for c in mock_send.call_args_list:
                msg = c[0][1] if len(c[0]) > 1 else {}
                if isinstance(msg, dict):
                    actions_sent.append(msg.get("action"))
            assert "resume" not in actions_sent, f"Resume should not be sent for already_hijacked. Got: {actions_sent}"

    async def test_compensating_resume_message_type_is_control(self) -> None:
        """mutmut_102-105: compensating resume msg type must be 'control'."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        sent_msgs: list[dict] = []

        async def capture(worker_id, msg):
            sent_msgs.append(msg)
            return True

        with (
            patch.object(hub, "try_acquire_ws_hijack", new_callable=AsyncMock, return_value=(False, "no_worker")),
            patch.object(hub, "send_worker", side_effect=capture),
        ):
            await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        resume_calls = [m for m in sent_msgs if m.get("action") == "resume"]
        assert resume_calls, "No resume message sent"
        assert resume_calls[0]["type"] == "control"

    async def test_compensating_resume_message_action_is_resume(self) -> None:
        """mutmut_106-109: compensating resume action must be 'resume'."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        sent_msgs: list[dict] = []

        async def capture(worker_id, msg):
            sent_msgs.append(msg)
            return True

        with (
            patch.object(hub, "try_acquire_ws_hijack", new_callable=AsyncMock, return_value=(False, "no_worker")),
            patch.object(hub, "send_worker", side_effect=capture),
        ):
            await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        resume_calls = [m for m in sent_msgs if m.get("action") == "resume"]
        assert resume_calls, "No resume message sent"
        assert resume_calls[0]["action"] == "resume"

    async def test_compensating_resume_message_owner_is_dashboard(self) -> None:
        """mutmut_110-113: compensating resume 'owner' must be 'dashboard'."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        sent_msgs: list[dict] = []

        async def capture(worker_id, msg):
            sent_msgs.append(msg)
            return True

        with (
            patch.object(hub, "try_acquire_ws_hijack", new_callable=AsyncMock, return_value=(False, "no_worker")),
            patch.object(hub, "send_worker", side_effect=capture),
        ):
            await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        resume_calls = [m for m in sent_msgs if m.get("action") == "resume"]
        assert resume_calls
        assert resume_calls[0]["owner"] == "dashboard"

    async def test_compensating_resume_message_lease_s_is_zero(self) -> None:
        """mutmut_114-116: compensating resume 'lease_s' must be 0 (not 1)."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        sent_msgs: list[dict] = []

        async def capture(worker_id, msg):
            sent_msgs.append(msg)
            return True

        with (
            patch.object(hub, "try_acquire_ws_hijack", new_callable=AsyncMock, return_value=(False, "no_worker")),
            patch.object(hub, "send_worker", side_effect=capture),
        ):
            await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        resume_calls = [m for m in sent_msgs if m.get("action") == "resume"]
        assert resume_calls
        assert resume_calls[0]["lease_s"] == 0

    async def test_compensating_resume_message_has_ts(self) -> None:
        """mutmut_117,118: compensating resume must have 'ts' key."""
        hub = _make_hub()
        ws = _make_ws()
        wws = _make_ws()
        await _register(hub, "w1", ws, "admin", wws)
        sent_msgs: list[dict] = []

        async def capture(worker_id, msg):
            sent_msgs.append(msg)
            return True

        with (
            patch.object(hub, "try_acquire_ws_hijack", new_callable=AsyncMock, return_value=(False, "no_worker")),
            patch.object(hub, "send_worker", side_effect=capture),
        ):
            await handle_browser_message(hub, ws, "w1", "admin", {"type": "hijack_request"}, False)
        resume_calls = [m for m in sent_msgs if m.get("action") == "resume"]
        assert resume_calls
        assert "ts" in resume_calls[0]


# ---------------------------------------------------------------------------
# _handle_resume — token default and _on_resume args
# ---------------------------------------------------------------------------
