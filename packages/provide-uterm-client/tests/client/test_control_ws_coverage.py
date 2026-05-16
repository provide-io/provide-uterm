#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Coverage tests for client/control_ws.py missing lines."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from provide.uterm.control_channel import encode_control, encode_data

from provide.uterm.client.control_ws import (
    AsyncInlineWebSocketClient,
    LogicalFrameDecoder,
    SyncInlineWebSocketClient,
    _SyncTestWsConnection,
    connect_test_ws,
)


class TestLogicalFrameDecoderFinish:
    """Cover finish() paths (lines 43-49) including DataChunk and ControlChunk."""

    def test_finish_with_data_chunk(self) -> None:
        """Covers lines 43-49: finish() loop with DataChunk event."""
        decoder = LogicalFrameDecoder(role="browser")
        # Feed partial data so finish flushes it
        decoder._decoder._buffer = "hello"
        result = decoder.finish()
        # "hello" is plain data → DataChunk → mapped to term frame
        assert result == [{"type": "term", "data": "hello"}]

    def test_finish_with_control_chunk(self) -> None:
        """Covers lines 46-47: finish() loop with ControlChunk event."""
        decoder = LogicalFrameDecoder(role="worker")
        # Put a complete control frame into the buffer
        encoded = encode_control({"type": "done"})
        decoder._decoder._buffer = encoded
        result = decoder.finish()
        assert result == [{"type": "done"}]

    def test_finish_with_data_chunk_worker_role(self) -> None:
        """Covers line 48-49: DataChunk mapped to input for worker role."""
        decoder = LogicalFrameDecoder(role="worker")
        decoder._decoder._buffer = "keystrokes"
        result = decoder.finish()
        assert result == [{"type": "input", "data": "keystrokes"}]


class TestSyncInlineWebSocketClientSendJson:
    """Cover send_json non-Mapping error path (line 72)."""

    def test_send_json_raises_for_non_mapping(self) -> None:
        """Covers line 72: TypeError when data is not a Mapping."""
        ws = MagicMock()
        client = SyncInlineWebSocketClient(ws, role="browser")
        with pytest.raises(TypeError, match="expected mapping payload"):
            client.send_json(["not", "a", "mapping"])

    def test_send_json_forwards_mapping(self) -> None:
        """Covers normal send_json path."""
        ws = MagicMock()
        client = SyncInlineWebSocketClient(ws, role="browser")
        client.send_json({"type": "ping"})
        ws.send_text.assert_called_once()


class TestAsyncInlineWebSocketClientGetattr:
    """Cover __getattr__ on AsyncInlineWebSocketClient (line 94)."""

    async def test_getattr_delegates_to_underlying_ws(self) -> None:
        """Covers line 94: __getattr__ returns attr from underlying ws."""
        ws = AsyncMock()
        ws.some_attr = "delegate_value"
        client = AsyncInlineWebSocketClient(ws, role="browser")
        assert client.some_attr == "delegate_value"


class TestAsyncSendJson:
    """Cover async send_json non-Mapping error path (lines 100-102)."""

    async def test_send_json_raises_for_non_mapping(self) -> None:
        """Covers lines 100-102: TypeError when data is not a Mapping."""
        ws = AsyncMock()
        client = AsyncInlineWebSocketClient(ws, role="browser")
        with pytest.raises(TypeError, match="expected mapping payload"):
            await client.send_json("not-a-mapping")

    async def test_send_json_forwards_mapping(self) -> None:
        """Covers normal async send_json path."""
        ws = AsyncMock()
        client = AsyncInlineWebSocketClient(ws, role="worker")
        await client.send_json({"type": "control"})
        ws.send.assert_awaited_once()


class TestAsyncSend:
    """Cover async send() dispatch paths (lines 104-120)."""

    async def test_send_bytes_passes_through(self) -> None:
        """Covers lines 105-107: bytes payload is sent directly."""
        ws = AsyncMock()
        client = AsyncInlineWebSocketClient(ws, role="browser")
        await client.send(b"raw-bytes")
        ws.send.assert_awaited_once_with(b"raw-bytes")

    async def test_send_str_non_json_passes_through(self) -> None:
        """Covers lines 108-113: str that is not valid JSON is sent directly."""
        ws = AsyncMock()
        client = AsyncInlineWebSocketClient(ws, role="browser")
        await client.send("not-json-{{{{")
        ws.send.assert_awaited_once_with("not-json-{{{{")

    async def test_send_str_json_mapping_encodes_as_frame(self) -> None:
        """Covers lines 108-116: str containing JSON object → encode as frame."""
        ws = AsyncMock()
        client = AsyncInlineWebSocketClient(ws, role="worker")
        payload = {"type": "input", "data": "x"}
        await client.send(json.dumps(payload))
        ws.send.assert_awaited_once_with(encode_data("x"))

    async def test_send_str_json_non_mapping_passes_through(self) -> None:
        """Covers lines 108-120: str with JSON non-object falls through to ws.send."""
        ws = AsyncMock()
        client = AsyncInlineWebSocketClient(ws, role="browser")
        # JSON array — not a Mapping, so no send_frame, falls to final ws.send
        await client.send(json.dumps([1, 2, 3]))
        ws.send.assert_awaited_once_with(json.dumps([1, 2, 3]))

    async def test_send_mapping_directly(self) -> None:
        """Covers lines 117-119: Mapping passed directly → send_frame."""
        ws = AsyncMock()
        client = AsyncInlineWebSocketClient(ws, role="browser")
        await client.send({"type": "ping"})
        ws.send.assert_awaited_once()

    async def test_send_non_str_non_bytes_non_mapping_falls_through(self) -> None:
        """Covers line 120: non-bytes, non-str, non-Mapping falls to ws.send."""
        ws = AsyncMock()
        client = AsyncInlineWebSocketClient(ws, role="browser")
        await client.send(42)
        ws.send.assert_awaited_once_with(42)


class TestAsyncRecv:
    """Cover recv() (line 132) and AsyncInlineWebSocketClient.recv() (line 157... actually 134-136)."""

    async def test_recv_returns_json_encoded_frame(self) -> None:
        """Covers lines 134-136: recv() calls recv_frame and json.dumps the result."""
        ws = AsyncMock()
        ws.recv.return_value = encode_control({"type": "hello", "ok": True})
        client = AsyncInlineWebSocketClient(ws, role="browser")
        result = await client.recv()
        parsed = json.loads(result)
        assert parsed == {"type": "hello", "ok": True}


class TestSyncReceiveJson:
    """Cover receive_json (line 81-82) and recv_frame pending path."""

    def test_receive_json_returns_frame(self) -> None:
        """Covers lines 81-82: receive_json delegates to recv_frame."""
        ws = MagicMock()
        ws.receive_text.return_value = encode_control({"type": "ack"})
        client = SyncInlineWebSocketClient(ws, role="browser")
        result = client.receive_json()
        assert result == {"type": "ack"}

    def test_recv_frame_returns_pending_without_ws_call(self) -> None:
        """Covers line 78: pending queue is used before calling ws."""
        ws = MagicMock()
        client = SyncInlineWebSocketClient(ws, role="browser")
        # Pre-load pending
        client._pending = [{"type": "cached"}]
        result = client.recv_frame()
        assert result == {"type": "cached"}
        ws.receive_text.assert_not_called()


class TestSyncTestWsConnection:
    """Cover _SyncTestWsConnection.__getattr__ AttributeError path (line 165-166)."""

    def test_getattr_raises_when_not_entered(self) -> None:
        """Covers lines 165-166: __getattr__ raises AttributeError when _ws is None."""
        conn = _SyncTestWsConnection(
            client=MagicMock(),
            url="/ws/browser/x",
            args=(),
            kwargs={},
            role="browser",
        )
        # _ws is None before __enter__
        with pytest.raises(AttributeError):
            _ = conn.some_attribute

    def test_exit_with_ctx_none_returns_none(self) -> None:
        """Covers line 157: __exit__ when _ctx is None returns None."""
        conn = _SyncTestWsConnection(
            client=MagicMock(),
            url="/ws/browser/x",
            args=(),
            kwargs={},
            role="browser",
        )
        # _ctx is None, so __exit__ should return None immediately
        result = conn.__exit__(None, None, None)
        assert result is None


class TestConnectTestWs:
    """Cover connect_test_ws URL-based role inference."""

    def test_worker_url_infers_worker_role(self) -> None:
        """Covers role inference: /ws/worker/ → 'worker' role."""
        client = MagicMock()
        conn = connect_test_ws(client, "/ws/worker/abc")
        assert conn._role == "worker"

    def test_browser_url_infers_browser_role(self) -> None:
        """Covers role inference: other URL → 'browser' role."""
        client = MagicMock()
        conn = connect_test_ws(client, "/ws/browser/abc")
        assert conn._role == "browser"

    def test_explicit_role_overrides_inference(self) -> None:
        """Covers explicit role parameter."""
        client = MagicMock()
        conn = connect_test_ws(client, "/ws/worker/abc", role="browser")
        assert conn._role == "browser"


class TestSyncInlineWebSocketClientGetattr:
    """Cover SyncInlineWebSocketClient.__getattr__ (line 65)."""

    def test_getattr_delegates_to_underlying_ws(self) -> None:
        """Covers line 65: __getattr__ returns attribute from underlying ws."""
        ws = MagicMock()
        ws.close_code = 1000
        client = SyncInlineWebSocketClient(ws, role="browser")
        assert client.close_code == 1000


class TestAsyncReceiveJson:
    """Cover AsyncInlineWebSocketClient.receive_json (line 132)."""

    async def test_receive_json_delegates_to_recv_frame(self) -> None:
        """Covers line 132: receive_json calls recv_frame."""
        ws = AsyncMock()
        ws.recv.return_value = encode_control({"type": "snapshot"})
        client = AsyncInlineWebSocketClient(ws, role="browser")
        result = await client.receive_json()
        assert result == {"type": "snapshot"}


class TestSyncTestWsConnectionEnterExit:
    """Cover _SyncTestWsConnection __enter__ and __exit__ paths (lines 150-162)."""

    def test_enter_wraps_ws_in_client(self) -> None:
        """Covers lines 150-153: __enter__ calls websocket_connect and wraps ws."""
        from provide.uterm.client.control_ws import SyncInlineWebSocketClient

        raw_ws = MagicMock()
        ctx_manager = MagicMock()
        ctx_manager.__enter__ = MagicMock(return_value=raw_ws)
        ctx_manager.__exit__ = MagicMock(return_value=None)

        test_client = MagicMock()
        test_client.websocket_connect.return_value = ctx_manager

        conn = _SyncTestWsConnection(
            client=test_client,
            url="/ws/browser/x",
            args=(),
            kwargs={},
            role="browser",
        )
        wrapped = conn.__enter__()
        assert isinstance(wrapped, SyncInlineWebSocketClient)
        test_client.websocket_connect.assert_called_once_with("/ws/browser/x")

    def test_exit_calls_ctx_exit_and_clears_state(self) -> None:
        """Covers lines 158-162: __exit__ calls ctx.__exit__ and clears _ctx/_ws."""
        raw_ws = MagicMock()
        ctx_manager = MagicMock()
        ctx_manager.__enter__ = MagicMock(return_value=raw_ws)
        ctx_manager.__exit__ = MagicMock(return_value=False)

        test_client = MagicMock()
        test_client.websocket_connect.return_value = ctx_manager

        conn = _SyncTestWsConnection(
            client=test_client,
            url="/ws/browser/x",
            args=(),
            kwargs={},
            role="browser",
        )
        conn.__enter__()
        assert conn._ctx is not None
        result = conn.__exit__(None, None, None)
        assert result is False
        assert conn._ctx is None
        assert conn._ws is None


class TestSyncTestWsConnectionGetattr:
    """Cover _SyncTestWsConnection.__getattr__ when _ws is not None (line 167)."""

    def test_getattr_delegates_to_ws_when_entered(self) -> None:
        """Covers line 167: __getattr__ delegates to _ws after __enter__."""
        raw_ws = MagicMock()
        raw_ws.some_prop = "value_from_ws"
        ctx_manager = MagicMock()
        ctx_manager.__enter__ = MagicMock(return_value=raw_ws)
        ctx_manager.__exit__ = MagicMock(return_value=None)

        test_client = MagicMock()
        test_client.websocket_connect.return_value = ctx_manager

        conn = _SyncTestWsConnection(
            client=test_client,
            url="/ws/browser/x",
            args=(),
            kwargs={},
            role="browser",
        )
        conn.__enter__()
        # After entering, _ws is set — __getattr__ should delegate to it
        assert conn.some_prop == "value_from_ws"


class TestConnectAsyncWs:
    """Cover connect_async_ws function (lines 179-183)."""

    async def test_connect_async_ws_wraps_websockets_connect(self) -> None:
        """Covers lines 179-183: connect_async_ws wraps websockets.connect."""
        from unittest.mock import patch

        from provide.uterm.client.control_ws import (
            AsyncInlineWebSocketClient,
            connect_async_ws,
        )

        mock_ws = AsyncMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch("websockets.connect", return_value=mock_ctx) as mock_connect:
            async with connect_async_ws("ws://example.com/ws/browser/x") as client:
                assert isinstance(client, AsyncInlineWebSocketClient)
                # _role is stored on the decoder, not on the client directly
                assert client._decoder._role == "browser"
            mock_connect.assert_called_once_with("ws://example.com/ws/browser/x")

    async def test_connect_async_ws_infers_worker_role(self) -> None:
        """Covers role inference in connect_async_ws."""
        from unittest.mock import patch

        from provide.uterm.client.control_ws import (
            AsyncInlineWebSocketClient,
            connect_async_ws,
        )

        mock_ws = AsyncMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch("websockets.connect", return_value=mock_ctx):
            async with connect_async_ws("ws://host/ws/worker/abc") as client:
                assert isinstance(client, AsyncInlineWebSocketClient)
                assert client._decoder._role == "worker"
