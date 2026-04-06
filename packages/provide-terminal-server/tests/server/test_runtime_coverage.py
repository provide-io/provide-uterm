#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Coverage gap tests for server/runtime.py."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from provide.terminal.server.models import RecordingConfig, SessionDefinition
from provide.terminal.server.runtime import HostedSessionRuntime


def _make_definition(**kwargs: Any) -> SessionDefinition:
    defaults: dict[str, Any] = {
        "session_id": "test-session",
        "display_name": "Test Session",
        "connector_type": "shell",
        "auto_start": False,
    }
    defaults.update(kwargs)
    return SessionDefinition(**defaults)


def _make_runtime(definition: SessionDefinition | None = None, **kwargs: Any) -> HostedSessionRuntime:
    if definition is None:
        definition = _make_definition()
    return HostedSessionRuntime(
        definition,
        public_base_url="http://localhost:9999",
        recording=RecordingConfig(),
        **kwargs,
    )


def _make_connector() -> MagicMock:
    connector = MagicMock()
    connector.is_connected = MagicMock(return_value=False)
    connector.start = AsyncMock()
    connector.stop = AsyncMock()
    connector.get_snapshot = AsyncMock(return_value={"type": "snapshot", "screen": "test", "ts": 1.0})
    connector.get_analysis = AsyncMock(return_value="analysis text")
    connector.poll_messages = AsyncMock(return_value=[])
    connector.set_mode = AsyncMock(return_value=[])
    connector.handle_control = AsyncMock(return_value=[])
    connector.handle_input = AsyncMock(return_value=[])
    return connector


# ---------------------------------------------------------------------------
# runtime.py line 148->150 — connector.is_connected() returns True
# ---------------------------------------------------------------------------


class TestStartConnectorIsConnected:
    async def test_start_connector_sets_connected_when_is_connected_true(self) -> None:
        """Line 148->150: connector.is_connected() returns True → _connected=True."""
        runtime = _make_runtime()
        connector = _make_connector()
        connector.is_connected = MagicMock(return_value=True)  # Returns True

        with patch("provide.terminal.server.runtime.build_connector", return_value=connector):
            result = await runtime._start_connector()

        assert runtime._connected is True
        assert result is connector


# ---------------------------------------------------------------------------
# runtime.py line 206->204 — outbound type is not "snapshot" in poll_task loop
# ---------------------------------------------------------------------------


class TestBridgeSessionPollNonSnapshot:
    async def test_poll_non_snapshot_message_not_logged(self) -> None:
        """Line 206->204: outbound.get('type') != 'snapshot' → log_snapshot not called."""
        runtime = _make_runtime()
        connector = _make_connector()
        runtime._connector = connector

        logged_snapshots: list[dict] = []

        async def _mock_log_snapshot(msg: dict) -> None:
            logged_snapshots.append(msg)

        runtime._log_snapshot = _mock_log_snapshot  # type: ignore[method-assign]
        runtime._log_event = AsyncMock()  # type: ignore[method-assign]
        runtime._log_send = AsyncMock()  # type: ignore[method-assign]
        runtime._queue = asyncio.Queue()

        # poll_messages returns a non-snapshot message (e.g. analysis)
        call_count = 0

        async def _poll() -> list[dict]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [{"type": "analysis", "formatted": "result", "ts": 1.0}]
            # After first poll, stop the runtime
            runtime._stop.set()
            return []

        connector.poll_messages = _poll

        mock_ws = AsyncMock()
        sent_msgs: list[str] = []

        async def mock_send(data: str) -> None:
            sent_msgs.append(data)

        mock_ws.send = mock_send
        mock_ws.recv = AsyncMock(side_effect=asyncio.CancelledError())

        connector.set_mode = AsyncMock(return_value=[])
        connector.get_snapshot = AsyncMock(return_value={"type": "snapshot", "screen": "", "ts": 1.0})

        # Run bridge_session briefly
        await runtime._bridge_session(mock_ws)

        # The analysis message should have been sent but not logged as snapshot
        analysis_sent = any("analysis" in m for m in sent_msgs)
        assert analysis_sent
        # log_snapshot should only have been called for the initial snapshot from get_snapshot
        # (which is put in the queue), not for the analysis message
        for logged in logged_snapshots:
            assert logged.get("type") == "snapshot"


# ---------------------------------------------------------------------------
# runtime.py line 233->237 — mtype doesn't match any branch (unknown type)
# ---------------------------------------------------------------------------


class TestBridgeSessionUnknownMtype:
    async def test_unknown_mtype_produces_no_responses(self) -> None:
        """Line 233->237: mtype not in known set → responses stays [], for loop skips."""
        runtime = _make_runtime()
        connector = _make_connector()
        runtime._connector = connector

        runtime._log_snapshot = AsyncMock()  # type: ignore[method-assign]
        runtime._log_event = AsyncMock()  # type: ignore[method-assign]
        runtime._log_send = AsyncMock()  # type: ignore[method-assign]
        runtime._queue = asyncio.Queue()

        async def _poll() -> list[dict]:
            return []

        connector.poll_messages = _poll

        sent_msgs: list[str] = []
        recv_call_count = 0

        async def mock_recv() -> str:
            nonlocal recv_call_count
            recv_call_count += 1
            if recv_call_count == 1:
                # Send an unknown message type
                return json.dumps({"type": "unknown_weird_type", "data": "whatever"})
            # Stop the runtime
            runtime._stop.set()
            raise asyncio.CancelledError

        async def mock_send(data: str) -> None:
            sent_msgs.append(data)

        mock_ws = AsyncMock()
        mock_ws.send = mock_send
        mock_ws.recv = mock_recv

        connector.set_mode = AsyncMock(return_value=[])
        connector.get_snapshot = AsyncMock(return_value={"type": "snapshot", "screen": "", "ts": 1.0})

        await runtime._bridge_session(mock_ws)

        # No response sent for unknown mtype (only initial snapshot from queue)
        # The for loop at line 237 iterates over empty responses list
        unknown_msgs = [m for m in sent_msgs if "unknown_weird_type" in m]
        assert len(unknown_msgs) == 0


# ---------------------------------------------------------------------------
# Regression: empty poll_messages() must not hot-spin (backoff sleep)
# ---------------------------------------------------------------------------


class TestPollEmptyBackoff:
    async def test_empty_poll_triggers_backoff_sleep(self) -> None:
        """When poll_messages() returns [] instantly, the loop must sleep to
        avoid hot-spinning.  Before the fix, connectors like shell/pty that
        return [] with no internal wait caused 99% CPU usage."""
        runtime = _make_runtime()
        connector = _make_connector()
        runtime._connector = connector

        runtime._log_snapshot = AsyncMock()  # type: ignore[method-assign]
        runtime._log_event = AsyncMock()  # type: ignore[method-assign]
        runtime._log_send = AsyncMock()  # type: ignore[method-assign]
        runtime._queue = asyncio.Queue()

        poll_count = 0

        async def _poll() -> list[dict]:
            nonlocal poll_count
            poll_count += 1
            if poll_count >= 3:
                runtime._stop.set()
            return []  # empty — should trigger backoff

        connector.poll_messages = _poll

        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        # recv blocks forever (simulates no client data)
        async def _recv_forever() -> str:
            await asyncio.sleep(999)
            return ""  # never reached

        mock_ws.recv = _recv_forever

        connector.set_mode = AsyncMock(return_value=[])
        connector.get_snapshot = AsyncMock(return_value={"type": "snapshot", "screen": "", "ts": 1.0})

        sleep_calls: list[float] = []
        original_sleep = asyncio.sleep

        async def _track_sleep(delay: float, *a: object, **kw: object) -> None:
            sleep_calls.append(delay)
            await original_sleep(delay)

        with patch("asyncio.sleep", side_effect=_track_sleep):
            await runtime._bridge_session(mock_ws)

        # The backoff sleep of 0.05s should have been called at least once
        backoff_sleeps = [d for d in sleep_calls if 0.04 <= d <= 0.06]
        assert len(backoff_sleeps) >= 1, (
            f"Expected backoff sleeps of ~0.05s but got: {sleep_calls}"
        )
