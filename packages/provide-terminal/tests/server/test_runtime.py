#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for server runtime.py — HostedSessionRuntime coverage gaps."""

from __future__ import annotations

import asyncio
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

    async def test_recording_wire_mode_passed_to_logger(self, tmp_path: Path) -> None:
        session = SessionDefinition(
            session_id="wire-rec",
            display_name="Wire Recording Test",
            connector_type="shell",
            recording_enabled=True,
            auto_start=False,
        )
        recording = RecordingConfig(
            enabled_by_default=True,
            directory=tmp_path,
            max_bytes=10_000,
            control_channel_mode="wire",
        )
        rt = HostedSessionRuntime(session, public_base_url="http://localhost:9999", recording=recording)

        connector = _make_connector()
        with patch("provide.terminal.server.runtime.build_connector", return_value=connector):
            await rt._start_connector()

        assert rt._logger is not None
        assert rt._logger._control_channel_mode == "wire"

        await rt._stop_connector()


# (_bridge_session and _run tests moved to test_runtime_2.py)
