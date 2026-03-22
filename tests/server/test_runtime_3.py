#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Coverage tests for server/runtime.py — remaining branch/line gaps.

Covers:
- line 36: _encode_runtime_frame with type=="term"
- branch 202->exit: _log_wire_send where type=="term" (log_control NOT called)
- lines 257-258: ControlStreamProtocolError raised by decoder.feed()
- branch 278->260: unknown mtype in _bridge_session (no matching elif)
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.terminal.control_stream import (
    ControlStreamProtocolError,
    encode_control,
    encode_data,
)
from provide.terminal.server.models import RecordingConfig, SessionDefinition
from provide.terminal.server.runtime import HostedSessionRuntime, _encode_runtime_frame

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_connector() -> MagicMock:
    connector = AsyncMock()
    connector.is_connected = MagicMock(return_value=True)
    connector.set_mode = AsyncMock(return_value=[])
    connector.get_snapshot = AsyncMock(return_value={"type": "snapshot", "screen": "test", "ts": 0.0})
    connector.poll_messages = AsyncMock(return_value=[])
    connector.handle_input = AsyncMock(return_value=[])
    connector.handle_control = AsyncMock(return_value=[])
    connector.clear = AsyncMock(return_value=[])
    connector.get_analysis = AsyncMock(return_value="no analysis")
    connector.stop = AsyncMock()
    return connector


class _MockWS:
    """WS mock: delivers messages then stops the bridge loop via _stop event."""

    def __init__(self, rt: HostedSessionRuntime, messages: list[str] | None = None) -> None:
        self._rt = rt
        self._messages = list(messages or [])
        self._msg_idx = 0
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        if self._msg_idx < len(self._messages):
            msg = self._messages[self._msg_idx]
            self._msg_idx += 1
            return msg
        # All messages delivered — stop the loop on the next iteration
        self._rt._stop.set()
        await asyncio.sleep(100)
        return ""


# ---------------------------------------------------------------------------
# _encode_runtime_frame: line 36 (type == "term")
# ---------------------------------------------------------------------------


class TestEncodeRuntimeFrame:
    def test_term_type_encodes_as_data(self) -> None:
        """Line 36: type=='term' → encode_data(msg['data']) returned."""
        msg = {"type": "term", "data": "hello world"}
        result = _encode_runtime_frame(msg)
        # encode_data just escapes DLE; for plain text it's identity
        assert result == encode_data("hello world")

    def test_term_type_empty_data(self) -> None:
        """Line 36: type=='term' with missing data → encode_data('')."""
        msg = {"type": "term"}
        result = _encode_runtime_frame(msg)
        assert result == encode_data("")

    def test_non_term_type_encodes_as_control(self) -> None:
        """Line 37 (else branch): type!='term' → encode_control(msg)."""
        msg = {"type": "snapshot", "screen": "x"}
        result = _encode_runtime_frame(msg)
        assert result == encode_control(msg)

    def test_no_type_encodes_as_control(self) -> None:
        """Line 37: missing type treated as non-term → encode_control."""
        msg = {"foo": "bar"}
        result = _encode_runtime_frame(msg)
        assert result == encode_control(msg)


# ---------------------------------------------------------------------------
# _log_wire_send: branch 202->exit (type=="term" → log_control NOT called)
# ---------------------------------------------------------------------------


class TestLogWireSendTermBranch:
    @pytest.mark.asyncio
    async def test_term_type_does_not_call_log_control(self) -> None:
        """Branch 202->exit: msg type=='term' → if condition False → log_control skipped."""
        rt = _make_runtime()
        mock_logger = AsyncMock()
        rt._logger = mock_logger

        await rt._log_wire_send("hello", {"type": "term", "data": "hello"})

        # log_wire should be called
        mock_logger.log_wire.assert_called_once_with("send", "hello")
        # log_control should NOT be called (type is "term")
        mock_logger.log_control.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_term_type_calls_log_control(self) -> None:
        """Contrasting test: non-term type → log_control IS called."""
        rt = _make_runtime()
        mock_logger = AsyncMock()
        rt._logger = mock_logger

        msg = {"type": "snapshot", "screen": "x"}
        await rt._log_wire_send("encoded", msg)

        mock_logger.log_wire.assert_called_once_with("send", "encoded")
        mock_logger.log_control.assert_called_once_with("send", msg)

    @pytest.mark.asyncio
    async def test_logger_none_returns_early(self) -> None:
        """Line 199->200: logger is None → return early, no log_control."""
        rt = _make_runtime()
        rt._logger = None
        # Should not raise
        await rt._log_wire_send("payload", {"type": "term"})


# ---------------------------------------------------------------------------
# _bridge_session: lines 257-258 (ControlStreamProtocolError)
# ---------------------------------------------------------------------------


class TestBridgeSessionControlStreamError:
    @pytest.mark.asyncio
    async def test_invalid_control_stream_raises_runtime_error(self) -> None:
        """Lines 257-258: ControlStreamProtocolError from decoder.feed() → RuntimeError raised."""
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop = asyncio.Event()
        connector = _make_connector()

        # Make poll slow so recv always wins the FIRST_COMPLETED race
        async def _slow_poll() -> list[dict[str, Any]]:
            await asyncio.sleep(10)
            return []

        connector.poll_messages = _slow_poll
        rt._connector = connector

        # DLE (\x10) followed by a non-STX, non-DLE byte is an invalid control prefix.
        invalid_payload = "\x10\x01garbage"

        ws = _MockWS(rt, messages=[invalid_payload])
        with pytest.raises(RuntimeError, match="invalid control stream"):
            await rt._bridge_session(ws)

    @pytest.mark.asyncio
    async def test_invalid_control_stream_via_decoder_patch(self) -> None:
        """Lines 257-258: RuntimeError raised from ControlStreamProtocolError (via patch)."""
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop = asyncio.Event()
        connector = _make_connector()

        # Make poll slow so recv always wins
        async def _slow_poll() -> list[dict[str, Any]]:
            await asyncio.sleep(10)
            return []

        connector.poll_messages = _slow_poll
        rt._connector = connector

        # Patch ControlStreamDecoder.feed to raise ControlStreamProtocolError
        with patch("provide.terminal.server.runtime.ControlStreamDecoder") as mock_decoder_cls:
            mock_decoder = MagicMock()
            mock_decoder_cls.return_value = mock_decoder
            mock_decoder.feed.side_effect = ControlStreamProtocolError("bad frame")

            ws = _MockWS(rt, messages=["any input"])
            with pytest.raises(RuntimeError, match="invalid control stream"):
                await rt._bridge_session(ws)


# ---------------------------------------------------------------------------
# _bridge_session: branch 278->260 (unknown mtype, not snapshot_req/analyze_req/control)
# ---------------------------------------------------------------------------


class TestBridgeSessionUnknownMtype:
    @pytest.mark.asyncio
    async def test_unknown_mtype_ignored_no_response(self) -> None:
        """Branch 278->260: elif mtype=='control' is False for unknown type → nothing added to responses."""
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop = asyncio.Event()
        connector = _make_connector()

        # Make poll slow so recv always wins the FIRST_COMPLETED race
        async def _slow_poll() -> list[dict[str, Any]]:
            await asyncio.sleep(10)
            return []

        connector.poll_messages = _slow_poll
        rt._connector = connector

        # Encode an unknown control type that is not snapshot_req, analyze_req, or control
        unknown_ctrl_msg = encode_control({"type": "unknown_op", "payload": "ignored"})

        ws = _MockWS(rt, messages=[unknown_ctrl_msg])
        await rt._bridge_session(ws)

        # handle_control, get_analysis NOT called for unknown type
        connector.handle_control.assert_not_called()
        # get_snapshot is called once during startup only
        assert connector.get_snapshot.call_count == 1
        connector.get_analysis.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_mtype_no_crash(self) -> None:
        """Branch 278->260: bridge_session completes normally when mtype is unrecognized."""
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop = asyncio.Event()
        connector = _make_connector()

        # Make poll slow so recv always wins
        async def _slow_poll() -> list[dict[str, Any]]:
            await asyncio.sleep(10)
            return []

        connector.poll_messages = _slow_poll
        rt._connector = connector

        # Multiple unrecognized control messages
        msgs = [
            encode_control({"type": "ping"}),
            encode_control({"type": "custom_event", "data": "x"}),
        ]
        ws = _MockWS(rt, messages=msgs)
        # Should complete without error
        await rt._bridge_session(ws)
