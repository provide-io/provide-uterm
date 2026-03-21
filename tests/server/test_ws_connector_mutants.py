#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for server connectors — WebSocket connector additions."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Helpers — WebSocket
# ---------------------------------------------------------------------------


def _make_ws(config: dict[str, Any] | None = None) -> Any:
    from provide.terminal.server.connectors.websocket import WebSocketSessionConnector

    return WebSocketSessionConnector(
        "ws-sess2",
        "Test WS2",
        config or {"url": "ws://localhost:9999/ws"},
    )


class TestWebSocketAdditionalMutants:
    """Additional websocket mutant tests that may not be covered by the existing file."""

    def test_ws_snapshot_prompt_id_is_ws_stream(self) -> None:
        """mutmut_50-56: prompt_id must be 'ws_stream', not variant strings."""
        c = _make_ws()
        snap = c._snapshot()
        assert snap["prompt_detected"]["prompt_id"] == "ws_stream"
        assert snap["prompt_detected"]["prompt_id"] != "ws_Stream"
        assert snap["prompt_detected"]["prompt_id"] != "WS_STREAM"

    def test_ws_snapshot_ts_key_not_variant(self) -> None:
        """mutmut_57,58: 'ts' key must be present, not 'XXtsXX' or 'TS'."""
        c = _make_ws()
        snap = c._snapshot()
        assert "ts" in snap
        assert "XXtsXX" not in snap
        assert "TS" not in snap

    @pytest.mark.asyncio
    async def test_ws_poll_connection_error_sets_connected_false(self) -> None:
        """mutmut_10: on exception, _connected must be False, not None."""
        c = _make_ws()
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=ConnectionError("lost"))
        c._ws = mock_ws
        c._connected = True
        await c.poll_messages()
        assert c._connected is False
        assert c._connected is not None

    @pytest.mark.asyncio
    async def test_ws_poll_connection_error_sets_banner(self) -> None:
        """mutmut_13,14,15: banner must be set on error, not left unchanged."""
        c = _make_ws()
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=ConnectionError("lost"))
        c._ws = mock_ws
        c._connected = True
        original_banner = c._banner
        await c.poll_messages()
        # Banner must be updated (contains "closed" or similar)
        assert c._banner != original_banner or "closed" in c._banner.lower()
        assert c._banner == "WebSocket connection closed." or "closed" in c._banner.lower()

    @pytest.mark.asyncio
    async def test_ws_poll_accumulates_bytes_for_binary(self) -> None:
        """mutmut_20: bytes for binary data must accumulate (+=), not assign (=)."""
        c = _make_ws()
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(return_value=b"\x00\x01\x02")
        c._ws = mock_ws
        c._connected = True
        c._received_bytes = 10
        await c.poll_messages()
        assert c._received_bytes == 13  # 10 + 3

    @pytest.mark.asyncio
    async def test_ws_poll_accumulates_bytes_for_text(self) -> None:
        """mutmut_23: bytes for text data must accumulate (+=), not assign (=)."""
        c = _make_ws()
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(return_value="abc")
        c._ws = mock_ws
        c._connected = True
        c._received_bytes = 10
        await c.poll_messages()
        assert c._received_bytes == 13  # 10 + 3 (len("abc".encode("utf-8")))

    def test_ws_hello_input_mode_key(self) -> None:
        """mutmut_7: hello must have 'input_mode' key."""
        c = _make_ws({"url": "ws://x", "input_mode": "hijack"})
        hello = c._hello()
        assert "input_mode" in hello
        assert hello["input_mode"] == "hijack"

    def test_ws_hello_ts_key(self) -> None:
        """mutmut_8: hello must have 'ts' key."""
        c = _make_ws()
        hello = c._hello()
        assert "ts" in hello
        assert isinstance(hello["ts"], float)

    @pytest.mark.asyncio
    async def test_ws_stop_connected_false_not_true(self) -> None:
        """mutmut_5: _connected must be False (not True) after stop."""
        c = _make_ws()
        c._ws = AsyncMock()
        c._connected = True
        await c.stop()
        assert c._connected is False
        assert not c.is_connected()

    @pytest.mark.asyncio
    async def test_ws_get_analysis_uses_newline(self) -> None:
        """mutmut_2: analysis must use '\n' not 'XX\nXX'."""
        c = _make_ws({"url": "ws://myws.example"})
        result = await c.get_analysis()
        assert "XX\nXX" not in result
        assert "\n" in result
        assert "ws-sess2" in result

    @pytest.mark.asyncio
    async def test_ws_clear_banner_exact(self) -> None:
        """mutmut_3,4,5,6: banner must be 'Screen buffer cleared.' exactly."""
        c = _make_ws()
        await c.clear()
        assert c._banner == "Screen buffer cleared."

    @pytest.mark.asyncio
    async def test_ws_handle_control_step_recognized(self) -> None:
        """mutmut_20,21: 'step' action must be matched exactly."""
        c = _make_ws()
        await c.handle_control("step")
        assert c._banner == "Step requested. Awaiting upstream output."
        assert "Ignored" not in c._banner

    @pytest.mark.asyncio
    async def test_ws_handle_control_step_banner_exact(self) -> None:
        """mutmut_22,23,24,25: step banner must be exact case."""
        c = _make_ws()
        await c.handle_control("step")
        assert c._banner == "Step requested. Awaiting upstream output."
