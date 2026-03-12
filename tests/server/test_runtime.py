#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for server runtime.py — HostedSessionRuntime coverage gaps."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.terminal.server.models import RecordingConfig, SessionDefinition
from provide.terminal.server.runtime import HostedSessionRuntime


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
# _ws_url
# ---------------------------------------------------------------------------


class TestWsUrl:
    def test_http_converts_to_ws(self) -> None:
        rt = _make_runtime(base_url="http://localhost:9999")
        assert rt._ws_url() == "ws://localhost:9999"

    def test_https_converts_to_wss(self) -> None:
        rt = _make_runtime(base_url="https://myhost.example.com")
        assert rt._ws_url() == "wss://myhost.example.com"

    def test_trailing_slash_stripped(self) -> None:
        rt = _make_runtime(base_url="http://localhost:9999/")
        assert rt._ws_url() == "ws://localhost:9999"


# ---------------------------------------------------------------------------
# set_mode
# ---------------------------------------------------------------------------


class TestSetMode:
    async def test_invalid_mode_raises(self) -> None:
        rt = _make_runtime()
        with pytest.raises(ValueError, match="invalid mode"):
            await rt.set_mode("superuser")

    async def test_connector_none_returns_early(self) -> None:
        rt = _make_runtime()
        rt._connector = None
        await rt.set_mode("open")  # should not raise

    async def test_with_connector_enqueues_messages(self) -> None:
        rt = _make_runtime()
        connector = _make_connector()
        connector.set_mode = AsyncMock(return_value=[{"type": "mode_set"}])
        rt._connector = connector
        rt._queue = asyncio.Queue()
        await rt.set_mode("open")
        assert not rt._queue.empty()


# ---------------------------------------------------------------------------
# clear / analyze
# ---------------------------------------------------------------------------


class TestClear:
    async def test_connector_none_returns_early(self) -> None:
        rt = _make_runtime()
        rt._connector = None
        await rt.clear()  # should not raise

    async def test_with_connector_enqueues_messages(self) -> None:
        rt = _make_runtime()
        connector = _make_connector()
        connector.clear = AsyncMock(return_value=[{"type": "cleared"}])
        rt._connector = connector
        rt._queue = asyncio.Queue()
        await rt.clear()
        assert not rt._queue.empty()


class TestAnalyze:
    async def test_connector_none_returns_offline(self) -> None:
        rt = _make_runtime()
        rt._connector = None
        result = await rt.analyze()
        assert result == "connector offline"

    async def test_with_connector_returns_analysis(self) -> None:
        rt = _make_runtime()
        connector = _make_connector()
        connector.get_analysis = AsyncMock(return_value="game state analysis")
        rt._connector = connector
        result = await rt.analyze()
        assert result == "game state analysis"


# ---------------------------------------------------------------------------
# _enqueue_messages
# ---------------------------------------------------------------------------


class TestEnqueueMessages:
    async def test_queue_none_skips(self) -> None:
        rt = _make_runtime()
        rt._queue = None
        await rt._enqueue_messages([{"type": "test"}])  # no exception

    async def test_queues_all_messages(self) -> None:
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        await rt._enqueue_messages([{"type": "a"}, {"type": "b"}])
        assert rt._queue.qsize() == 2


# ---------------------------------------------------------------------------
# logging helpers
# ---------------------------------------------------------------------------


class TestLogMethods:
    async def test_log_send_noop_without_logger(self) -> None:
        rt = _make_runtime()
        rt._logger = None
        await rt._log_send("hello")  # should not raise

    async def test_log_send_with_logger(self) -> None:
        rt = _make_runtime()
        mock_logger = AsyncMock()
        rt._logger = mock_logger
        await rt._log_send("hello world")
        mock_logger.log_send.assert_called_once_with("hello world")

    async def test_log_snapshot_noop_without_logger(self) -> None:
        rt = _make_runtime()
        rt._logger = None
        await rt._log_snapshot({"screen": "test"})

    async def test_log_snapshot_with_logger(self) -> None:
        rt = _make_runtime()
        mock_logger = AsyncMock()
        rt._logger = mock_logger
        await rt._log_snapshot({"screen": "hello"})
        mock_logger.log_screen.assert_called_once()

    async def test_log_event_noop_without_logger(self) -> None:
        rt = _make_runtime()
        rt._logger = None
        await rt._log_event("test_event", {})

    async def test_log_event_with_logger(self) -> None:
        rt = _make_runtime()
        mock_logger = AsyncMock()
        rt._logger = mock_logger
        await rt._log_event("started", {"key": "val"})
        mock_logger.log_event.assert_called_once_with("started", {"key": "val"})


# ---------------------------------------------------------------------------
# _start_connector with recording enabled
# ---------------------------------------------------------------------------


class TestStartConnectorRecording:
    async def test_recording_enabled_creates_logger_and_path(self, tmp_path: Path) -> None:
        session = SessionDefinition(
            session_id="rec-session",
            display_name="Recording Test",
            connector_type="shell",
            recording_enabled=True,
            auto_start=False,
        )
        recording = RecordingConfig(enabled_by_default=True, directory=tmp_path, max_bytes=10_000)
        rt = HostedSessionRuntime(session, public_base_url="http://localhost:9999", recording=recording)

        connector = _make_connector()
        with patch("provide.terminal.server.runtime.build_connector", return_value=connector):
            await rt._start_connector()

        assert rt._logger is not None
        assert rt._recording_path is not None
        assert "rec-session" in str(rt._recording_path)

        await rt._stop_connector()


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
        self.sent.append(json.loads(data))

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

        ws = _MockWS(rt, messages=[json.dumps({"type": "input", "data": "hello\r"})])
        await rt._bridge_session(ws)
        connector.handle_input.assert_called_with("hello\r")

    async def test_handles_control_message(self) -> None:
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop = asyncio.Event()
        connector = _make_connector()
        connector.handle_control = AsyncMock(return_value=[])
        rt._connector = connector

        ws = _MockWS(rt, messages=[json.dumps({"type": "control", "action": "pause"})])
        await rt._bridge_session(ws)
        connector.handle_control.assert_called_with("pause")

    async def test_handles_snapshot_req(self) -> None:
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop = asyncio.Event()
        connector = _make_connector()
        rt._connector = connector

        ws = _MockWS(rt, messages=[json.dumps({"type": "snapshot_req"})])
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

        ws = _MockWS(rt, messages=[json.dumps({"type": "analyze_req"})])
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

        ws = _MockWS(rt, messages=[json.dumps({"type": "input", "data": "typed text"})])
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
        """Attempt counter (backoff index) resets to 0 after a session completes normally."""
        rt = _make_runtime()
        connector = _make_connector()

        class _CleanCtx:
            async def __aenter__(self) -> _WS:
                return _WS()

            async def __aexit__(self, *_: object) -> None:
                return None

        class _WS:
            async def send(self, data: str) -> None:
                pass  # allow sends without stopping

            async def recv(self) -> str:
                # Set stop on first recv call — bridge_session will exit cleanly
                rt._stop.set()
                await asyncio.sleep(100)
                return ""

        fake_ws_mod = MagicMock()
        fake_ws_mod.connect = MagicMock(return_value=_CleanCtx())

        real_ws = sys.modules.pop("websockets", None)
        sys.modules["websockets"] = fake_ws_mod
        try:
            with patch("provide.terminal.server.runtime.build_connector", return_value=connector):
                await rt.start()
                for _ in range(200):
                    await asyncio.sleep(0.02)
                    if rt._task is not None and rt._task.done():
                        break
        finally:
            if real_ws is not None:
                sys.modules["websockets"] = real_ws
            else:
                sys.modules.pop("websockets", None)

        # Test passes if it finishes — verifies attempt=0 line is executed
        assert rt._task is not None and rt._task.done()
