#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for HostedSessionRuntime — _bridge_session and _run coverage."""

from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.terminal.server.models import RecordingConfig, SessionDefinition
from provide.terminal.server.runtime import HostedSessionRuntime
from tests.helpers import decode_chunk, encode_frame


def _make_session(session_id: str = "test-session", connector_type: str = "shell") -> SessionDefinition:
    return SessionDefinition(
        session_id=session_id,
        display_name="Test Session",
        connector_type=connector_type,
        auto_start=False,
    )


def _make_runtime(
    session_id: str = "test-session",
    base_url: str = "http://localhost:9999",
) -> HostedSessionRuntime:
    return HostedSessionRuntime(
        _make_session(session_id),
        public_base_url=base_url,
        recording=RecordingConfig(),
    )


async def _slow_poll() -> list[dict[str, Any]]:
    """Default poll that yields once to let recv complete first, then returns empty."""
    await asyncio.sleep(0.05)
    return []


def _make_connector() -> MagicMock:
    connector = AsyncMock()
    connector.is_connected = MagicMock(return_value=True)
    connector.set_mode = AsyncMock(return_value=[])
    connector.get_snapshot = AsyncMock(return_value={"type": "snapshot", "screen": "test", "ts": 0.0})
    connector.poll_messages = _slow_poll  # delay so recv wins FIRST_COMPLETED race
    connector.handle_input = AsyncMock(return_value=[])
    connector.handle_control = AsyncMock(return_value=[])
    connector.clear = AsyncMock(return_value=[])
    connector.get_analysis = AsyncMock(return_value="no analysis")
    connector.stop = AsyncMock()
    return connector


# ---------------------------------------------------------------------------
# _bridge_session — message type paths and edge cases
# ---------------------------------------------------------------------------


class _MockWS:
    """WS mock: delivers messages then stops the bridge loop via _stop event.

    After all messages are delivered, the next recv() call sets rt._stop so the
    bridge loop exits on its next iteration (within ≤0.5s timeout).
    """

    def __init__(self, rt: HostedSessionRuntime, messages: list[str] | None = None) -> None:
        self._rt = rt
        self._messages = list(messages or [])
        self._msg_idx = 0
        self.sent: list[dict[str, Any]] = []

    async def send(self, data: str) -> None:
        self.sent.extend(decode_chunk(data, data_type="term"))

    async def recv(self) -> str:
        if self._msg_idx < len(self._messages):
            msg = self._messages[self._msg_idx]
            self._msg_idx += 1
            return msg
        # All messages delivered — stop the loop on the next iteration
        self._rt._stop.set()
        await asyncio.sleep(100)
        return ""


class TestBridgeSession:
    async def test_connector_none_raises(self) -> None:
        rt = _make_runtime()
        rt._connector = None
        rt._queue = asyncio.Queue()
        rt._stop = asyncio.Event()
        with pytest.raises(RuntimeError, match="connector unavailable"):
            await rt._bridge_session(_MockWS(rt))

    async def test_sends_initial_snapshot_on_connect(self) -> None:
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop = asyncio.Event()
        connector = _make_connector()
        rt._connector = connector

        ws = _MockWS(rt)
        await rt._bridge_session(ws)
        assert any(m.get("type") == "snapshot" for m in ws.sent)

    async def test_handles_input_message(self) -> None:
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop = asyncio.Event()
        connector = _make_connector()
        connector.handle_input = AsyncMock(return_value=[])
        rt._connector = connector

        ws = _MockWS(rt, messages=[encode_frame({"type": "input", "data": "hello\r"})])
        await rt._bridge_session(ws)
        connector.handle_input.assert_called_with("hello\r")

    async def test_handles_control_message(self) -> None:
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop = asyncio.Event()
        connector = _make_connector()
        connector.handle_control = AsyncMock(return_value=[])
        rt._connector = connector

        ws = _MockWS(rt, messages=[encode_frame({"type": "control", "action": "pause"})])
        await rt._bridge_session(ws)
        connector.handle_control.assert_called_with("pause")

    async def test_handles_snapshot_req(self) -> None:
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop = asyncio.Event()
        connector = _make_connector()
        rt._connector = connector

        ws = _MockWS(rt, messages=[encode_frame({"type": "snapshot_req"})])
        await rt._bridge_session(ws)
        # get_snapshot called at least once for startup + once for req
        assert connector.get_snapshot.call_count >= 2

    async def test_handles_analyze_req(self) -> None:
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop = asyncio.Event()
        connector = _make_connector()
        connector.get_analysis = AsyncMock(return_value="analysis result")
        rt._connector = connector

        ws = _MockWS(rt, messages=[encode_frame({"type": "analyze_req"})])
        await rt._bridge_session(ws)
        connector.get_analysis.assert_called()

    async def test_bad_json_from_browser_is_ignored(self) -> None:
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop = asyncio.Event()
        connector = _make_connector()
        rt._connector = connector

        ws = _MockWS(rt, messages=["not-json!!!"])
        # Should not raise
        await rt._bridge_session(ws)

    async def test_log_send_called_on_input(self) -> None:
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop = asyncio.Event()
        connector = _make_connector()
        rt._connector = connector
        mock_logger = AsyncMock()
        rt._logger = mock_logger

        ws = _MockWS(rt, messages=[encode_frame({"type": "input", "data": "typed text"})])
        await rt._bridge_session(ws)
        mock_logger.log_send.assert_called_with("typed text")

    async def test_bridge_session_timeout_continue(self) -> None:
        """Line 202: continue when asyncio.wait times out with no tasks completing."""
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop = asyncio.Event()
        connector = _make_connector()

        recv_calls = 0

        async def _timed_recv() -> str:
            nonlocal recv_calls
            recv_calls += 1
            if recv_calls == 1:
                # First call: slower than 0.5s timeout → will be cancelled
                await asyncio.sleep(1.0)
                return ""
            # Second call: set stop then block (will be cancelled by outer _run cleanup)
            rt._stop.set()
            await asyncio.sleep(100)
            return ""

        poll_calls = 0

        async def _timed_poll() -> list[dict[str, Any]]:
            nonlocal poll_calls
            poll_calls += 1
            if poll_calls == 1:
                # First call: slower than 0.5s timeout → will be cancelled
                await asyncio.sleep(1.0)
            return []

        connector.poll_messages = _timed_poll
        rt._connector = connector

        ws = MagicMock()
        ws.recv = _timed_recv
        ws.send = AsyncMock()

        await rt._bridge_session(ws)

        # Both timed out on iteration 1 (line 202 hit); iteration 2 exited via _stop
        assert recv_calls >= 2
        assert poll_calls >= 2

    async def test_poll_task_results_forwarded_to_browser(self) -> None:
        """Results from poll_task (connector outbound data) are sent to the browser."""
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop = asyncio.Event()
        connector = _make_connector()

        poll_call = [0]

        async def _poll() -> list[dict[str, Any]]:
            poll_call[0] += 1
            if poll_call[0] == 1:
                return [{"type": "snapshot", "screen": "polled", "ts": 0.0}]
            await asyncio.sleep(100)
            return []

        connector.poll_messages = _poll
        rt._connector = connector

        ws = _MockWS(rt)
        await rt._bridge_session(ws)
        assert any(m.get("screen") == "polled" for m in ws.sent)

    async def test_wire_mode_logs_wire_and_control_events(self) -> None:
        rt = _make_runtime()
        rt._recording_cfg.control_channel_mode = "wire"
        rt._queue = asyncio.Queue()
        rt._stop = asyncio.Event()
        connector = _make_connector()
        connector.handle_control = AsyncMock(return_value=[])
        rt._connector = connector
        mock_logger = AsyncMock()
        rt._logger = mock_logger

        ws = _MockWS(rt, messages=[encode_frame({"type": "control", "action": "pause"})])
        await rt._bridge_session(ws)

        wire_directions = [call.args[0] for call in mock_logger.log_wire.call_args_list]
        control_directions = [call.args[0] for call in mock_logger.log_control.call_args_list]
        assert "recv" in wire_directions
        assert "send" in wire_directions
        assert "recv" in control_directions
        assert "send" in control_directions


# ---------------------------------------------------------------------------
# _run — error handling and retry logic
# ---------------------------------------------------------------------------


class TestRun:
    async def test_value_error_stops_permanently(self) -> None:
        """ValueError in _run is treated as a permanent error — no retry."""
        rt = _make_runtime()
        call_count = [0]

        async def _bad_start() -> None:
            call_count[0] += 1
            raise ValueError("unsupported connector")

        with patch.object(rt, "_start_connector", _bad_start):
            await rt.start()
            for _ in range(50):
                await asyncio.sleep(0.02)
                if rt._task is not None and rt._task.done():
                    break

        # _run() always sets state="stopped" at exit — check _last_error and task.done()
        assert rt._task is not None and rt._task.done()
        assert rt._last_error == "unsupported connector"
        assert call_count[0] == 1  # one attempt, no retry

    async def test_value_error_logs_event(self) -> None:
        """ValueError is logged via _log_event with permanent=True."""
        rt = _make_runtime()
        mock_logger = AsyncMock()
        rt._logger = mock_logger

        async def _bad_start() -> None:
            raise ValueError("bad config")

        with patch.object(rt, "_start_connector", _bad_start):
            await rt.start()
            for _ in range(50):
                await asyncio.sleep(0.02)
                if rt._task is not None and rt._task.done():
                    break

        mock_logger.log_event.assert_called()
        call_args = mock_logger.log_event.call_args
        assert call_args[0][0] == "runtime_error"
        assert call_args[0][1].get("permanent") is True

    async def test_http_401_stops_permanently(self) -> None:
        """HTTP 401 from websockets.connect is treated as permanent — no retry."""
        rt = _make_runtime()
        connector = _make_connector()

        class FakeStatusError(Exception):
            status_code = 401

        class _FakeCtx:
            async def __aenter__(self) -> None:
                raise FakeStatusError("Unauthorized")

            async def __aexit__(self, *_: object) -> None:
                return None

        fake_ws_mod = MagicMock()
        fake_ws_mod.connect = MagicMock(return_value=_FakeCtx())

        real_ws = sys.modules.pop("websockets", None)
        sys.modules["websockets"] = fake_ws_mod
        try:
            with patch("provide.terminal.server.runtime.build_connector", return_value=connector):
                await rt.start()
                for _ in range(50):
                    await asyncio.sleep(0.02)
                    if rt._task is not None and rt._task.done():
                        break
        finally:
            if real_ws is not None:
                sys.modules["websockets"] = real_ws
            else:
                sys.modules.pop("websockets", None)

        assert rt._task is not None and rt._task.done()
        assert rt._last_error is not None

    async def test_http_403_stops_permanently(self) -> None:
        """HTTP 403 from websockets.connect is treated as permanent — no retry."""
        rt = _make_runtime()
        connector = _make_connector()

        class FakeStatusError(Exception):
            status_code = 403

        class _FakeCtx:
            async def __aenter__(self) -> None:
                raise FakeStatusError("Forbidden")

            async def __aexit__(self, *_: object) -> None:
                return None

        fake_ws_mod = MagicMock()
        fake_ws_mod.connect = MagicMock(return_value=_FakeCtx())

        real_ws = sys.modules.pop("websockets", None)
        sys.modules["websockets"] = fake_ws_mod
        try:
            with patch("provide.terminal.server.runtime.build_connector", return_value=connector):
                await rt.start()
                for _ in range(50):
                    await asyncio.sleep(0.02)
                    if rt._task is not None and rt._task.done():
                        break
        finally:
            if real_ws is not None:
                sys.modules["websockets"] = real_ws
            else:
                sys.modules.pop("websockets", None)

        assert rt._task is not None and rt._task.done()
        assert rt._last_error is not None

    async def test_http_404_stops_permanently(self) -> None:
        """HTTP 404 from websockets.connect is treated as permanent — no retry."""
        rt = _make_runtime()
        connector = _make_connector()

        class FakeStatusError(Exception):
            status_code = 404

        class _FakeCtx:
            async def __aenter__(self) -> None:
                raise FakeStatusError("Not Found")

            async def __aexit__(self, *_: object) -> None:
                return None

        fake_ws_mod = MagicMock()
        fake_ws_mod.connect = MagicMock(return_value=_FakeCtx())

        real_ws = sys.modules.pop("websockets", None)
        sys.modules["websockets"] = fake_ws_mod
        try:
            with patch("provide.terminal.server.runtime.build_connector", return_value=connector):
                await rt.start()
                for _ in range(50):
                    await asyncio.sleep(0.02)
                    if rt._task is not None and rt._task.done():
                        break
        finally:
            if real_ws is not None:
                sys.modules["websockets"] = real_ws
            else:
                sys.modules.pop("websockets", None)

        assert rt._task is not None and rt._task.done()
        assert rt._last_error is not None

    async def test_transient_error_retries(self) -> None:
        """Transient (non-HTTP) errors trigger backoff and retry."""
        rt = _make_runtime()
        call_count = [0]

        async def _flaky_start() -> None:
            call_count[0] += 1
            if call_count[0] < 3:
                raise ConnectionRefusedError("not yet")
            # Cancel to end the loop after proving we retried
            rt._stop.set()
            raise asyncio.CancelledError

        with patch.object(rt, "_start_connector", _flaky_start):
            await rt.start()
            for _ in range(200):
                await asyncio.sleep(0.01)
                if rt._task is not None and rt._task.done():
                    break

        assert call_count[0] >= 2  # retried at least once

    async def test_backoff_reset_after_clean_session(self) -> None:
        """Attempt counter (backoff index) resets to 0 after a session completes normally.

        Mocks _bridge_session directly so that line 324 (attempt = 0) is definitely hit.
        """
        rt = _make_runtime()
        connector = _make_connector()
        bridge_calls = [0]

        async def _fake_bridge(ws: Any) -> None:
            bridge_calls[0] += 1
            rt._stop.set()  # signal clean exit after first session

        class _CleanCtx:
            async def __aenter__(self) -> MagicMock:
                return MagicMock()

            async def __aexit__(self, *_: object) -> None:
                return None

        fake_ws_mod = MagicMock()
        fake_ws_mod.connect = MagicMock(return_value=_CleanCtx())

        real_ws = sys.modules.pop("websockets", None)
        sys.modules["websockets"] = fake_ws_mod
        try:
            with (
                patch("provide.terminal.server.runtime.build_connector", return_value=connector),
                patch.object(rt, "_bridge_session", _fake_bridge),
            ):
                await rt.start()
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if rt._task is not None and rt._task.done():
                        break
        finally:
            if real_ws is not None:
                sys.modules["websockets"] = real_ws
            else:
                sys.modules.pop("websockets", None)

        assert rt._task is not None and rt._task.done()
        assert bridge_calls[0] == 1
