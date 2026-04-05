#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for inline control-channel WebSocket clients."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.terminal.client.control_ws import (
    AsyncInlineWebSocketClient,
    LogicalFrameDecoder,
    SyncInlineWebSocketClient,
    encode_logical_frame,
)
from provide.terminal.control_channel import encode_control, encode_data


def test_encode_logical_frame_uses_data_channel_for_term_and_input() -> None:
    assert encode_logical_frame({"type": "term", "data": "abc"}) == encode_data("abc")
    assert encode_logical_frame({"type": "input", "data": "xyz"}) == encode_data("xyz")


def test_encode_logical_frame_uses_control_channel_for_other_frames() -> None:
    payload = {"type": "hello", "worker_online": True}
    assert encode_logical_frame(payload) == encode_control(payload)


class TestLogicalFrameDecoder:
    def test_browser_decoder_maps_data_to_term(self) -> None:
        decoder = LogicalFrameDecoder(role="browser")
        assert decoder.feed(encode_data("hello")) == [{"type": "term", "data": "hello"}]

    def test_worker_decoder_maps_data_to_input(self) -> None:
        decoder = LogicalFrameDecoder(role="worker")
        assert decoder.feed(encode_data("hello")) == [{"type": "input", "data": "hello"}]

    def test_decoder_preserves_control_frames(self) -> None:
        decoder = LogicalFrameDecoder(role="browser")
        assert decoder.feed(encode_control({"type": "ping"})) == [{"type": "ping"}]


class TestSyncInlineWebSocketClient:
    def test_send_frame_encodes_inline_protocol(self) -> None:
        ws = MagicMock()
        client = SyncInlineWebSocketClient(ws, role="browser")

        client.send_frame({"type": "input", "data": "abc"})

        ws.send_text.assert_called_once_with(encode_data("abc"))

    def test_recv_frame_decodes_control_and_data(self) -> None:
        ws = MagicMock()
        ws.receive_text.side_effect = [
            encode_control({"type": "hello", "worker_online": True}),
            encode_data("screen bytes"),
        ]
        client = SyncInlineWebSocketClient(ws, role="browser")

        assert client.recv_frame() == {"type": "hello", "worker_online": True}
        assert client.recv_frame() == {"type": "term", "data": "screen bytes"}


class TestAsyncInlineWebSocketClient:
    async def test_send_frame_encodes_inline_protocol(self) -> None:
        ws = AsyncMock()
        client = AsyncInlineWebSocketClient(ws, role="worker")

        await client.send_frame({"type": "control", "action": "pause"})

        ws.send.assert_awaited_once_with(encode_control({"type": "control", "action": "pause"}))

    async def test_recv_frame_decodes_pending_events(self) -> None:
        ws = AsyncMock()
        ws.recv.side_effect = [
            encode_control({"type": "hello", "worker_online": True}) + encode_data("typed"),
        ]
        client = AsyncInlineWebSocketClient(ws, role="worker")

        assert await client.recv_frame() == {"type": "hello", "worker_online": True}
        assert await client.recv_frame() == {"type": "input", "data": "typed"}

    async def test_recv_frame_rejects_binary_payloads(self) -> None:
        ws = AsyncMock()
        ws.recv.return_value = b"raw-bytes"
        client = AsyncInlineWebSocketClient(ws, role="browser")

        with pytest.raises(TypeError, match="expected text WebSocket payload"):
            await client.recv_frame()
