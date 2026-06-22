#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for WebSocketSessionConnector."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.server.connectors import KNOWN_CONNECTOR_TYPES, build_connector
from provide.uterm.server.connectors.websocket import WebSocketSessionConnector


class TestWebSocketSessionConnector:
    def _make(self, config: dict[str, Any] | None = None) -> WebSocketSessionConnector:
        return WebSocketSessionConnector(
            "ws-sess",
            "Test WS",
            config or {"url": "ws://localhost:9999/ws"},
        )

    # -- Config validation ---------------------------------------------------

    def test_unknown_config_key_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown websocket connector_config"):
            WebSocketSessionConnector("s", "n", {"url": "ws://x", "bogus": 1})

    def test_missing_url_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="requires connector_config.url"):
            WebSocketSessionConnector("s", "n", {})

    def test_invalid_url_scheme_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="url scheme"):
            WebSocketSessionConnector("s", "n", {"url": "https://x/ws"})

    def test_url_without_host_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="must include a host"):
            WebSocketSessionConnector("s", "n", {"url": "ws:///path"})

    def test_valid_config_accepted(self) -> None:
        c = self._make({"url": "ws://x", "input_mode": "open"})
        assert c._input_mode == "open"

    def test_default_input_mode(self) -> None:
        c = self._make()
        assert c._input_mode == "open"

    # -- Connection lifecycle ------------------------------------------------

    @pytest.mark.asyncio
    async def test_start_connects(self) -> None:
        mock_ws = AsyncMock()
        # Peer-IP egress guard reads ws.remote_address post-connect; a benign
        # public peer must let start() proceed.
        mock_ws.remote_address = ("93.184.216.34", 443)
        mock_mod = MagicMock()
        mock_mod.connect = AsyncMock(return_value=mock_ws)
        with patch.dict("sys.modules", {"websockets": mock_mod}):
            c = self._make()
            await c.start()
            mock_mod.connect.assert_awaited_once_with("ws://localhost:9999/ws")
            assert c.is_connected()
            assert c._ws is mock_ws

    @pytest.mark.asyncio
    async def test_start_missing_websockets_raises(self) -> None:
        import sys

        saved = sys.modules.pop("websockets", "MISSING")
        sys.modules["websockets"] = None  # type: ignore[assignment]
        try:
            c = self._make()
            with pytest.raises(ImportError, match="websocket connector requires"):
                await c.start()
        finally:
            if saved == "MISSING":
                sys.modules.pop("websockets", None)
            else:
                sys.modules["websockets"] = saved

    @pytest.mark.asyncio
    async def test_stop_closes(self) -> None:
        mock_ws = AsyncMock()
        c = self._make()
        c._ws = mock_ws
        c._connected = True
        await c.stop()
        mock_ws.close.assert_awaited_once()
        assert not c.is_connected()
        assert c._ws is None

    @pytest.mark.asyncio
    async def test_stop_tolerates_close_error(self) -> None:
        mock_ws = AsyncMock()
        mock_ws.close.side_effect = RuntimeError("gone")
        c = self._make()
        c._ws = mock_ws
        c._connected = True
        await c.stop()
        assert not c.is_connected()

    @pytest.mark.asyncio
    async def test_stop_when_not_connected(self) -> None:
        c = self._make()
        await c.stop()
        assert not c.is_connected()

    def test_is_connected_false_without_ws(self) -> None:
        c = self._make()
        c._connected = True
        c._ws = None
        assert not c.is_connected()

    # -- Polling -------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_poll_not_connected(self) -> None:
        c = self._make()
        assert await c.poll_messages() == []

    @pytest.mark.asyncio
    async def test_poll_timeout_returns_empty(self) -> None:
        mock_ws = AsyncMock()
        mock_ws.recv.side_effect = TimeoutError
        c = self._make()
        c._ws = mock_ws
        c._connected = True
        assert await c.poll_messages() == []

    @pytest.mark.asyncio
    async def test_poll_text_data(self) -> None:
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(return_value="hello world")
        c = self._make()
        c._ws = mock_ws
        c._connected = True
        msgs = await c.poll_messages()
        assert len(msgs) == 2
        assert msgs[0]["type"] == "term"
        assert msgs[0]["data"] == "hello world"
        assert msgs[1]["type"] == "snapshot"
        assert c._received_bytes == len(b"hello world")

    @pytest.mark.asyncio
    async def test_poll_bytes_data(self) -> None:
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(return_value=b"\xb0\xb1\xb2")
        c = self._make()
        c._ws = mock_ws
        c._connected = True
        msgs = await c.poll_messages()
        assert msgs[0]["type"] == "term"
        assert msgs[1]["type"] == "snapshot"
        assert c._received_bytes == 3

    @pytest.mark.asyncio
    async def test_poll_connection_error_marks_disconnected(self) -> None:
        mock_ws = AsyncMock()
        mock_ws.recv.side_effect = ConnectionError("closed")
        c = self._make()
        c._ws = mock_ws
        c._connected = True
        msgs = await c.poll_messages()
        assert not c._connected
        assert c._ws is None
        mock_ws.close.assert_awaited_once()
        assert len(msgs) == 1
        assert msgs[0]["type"] == "snapshot"
        assert "closed" in msgs[0]["screen"].lower()

    @pytest.mark.asyncio
    async def test_poll_connection_error_close_raises(self) -> None:
        """close() failure during error cleanup is suppressed."""
        mock_ws = AsyncMock()
        mock_ws.recv.side_effect = ConnectionError("gone")
        mock_ws.close.side_effect = RuntimeError("already closed")
        c = self._make()
        c._ws = mock_ws
        c._connected = True
        msgs = await c.poll_messages()
        assert not c._connected
        assert c._ws is None
        assert len(msgs) == 1

    @pytest.mark.asyncio
    async def test_poll_buffer_truncation(self) -> None:
        mock_ws = AsyncMock()
        big = "x" * 40_000
        mock_ws.recv = AsyncMock(return_value=big)
        c = self._make()
        c._ws = mock_ws
        c._connected = True
        await c.poll_messages()
        assert len(c._screen_buffer) == 32_000

    # -- Input handling ------------------------------------------------------

    @pytest.mark.asyncio
    async def test_handle_input_sends(self) -> None:
        mock_ws = AsyncMock()
        c = self._make()
        c._ws = mock_ws
        c._connected = True
        msgs = await c.handle_input("test keys")
        mock_ws.send.assert_awaited_once_with("test keys")
        assert msgs[-1]["type"] == "snapshot"

    @pytest.mark.asyncio
    async def test_handle_input_not_connected(self) -> None:
        c = self._make()
        msgs = await c.handle_input("test")
        assert msgs[-1]["type"] == "snapshot"

    # -- Control operations --------------------------------------------------

    @pytest.mark.asyncio
    async def test_handle_control_pause(self) -> None:
        c = self._make()
        msgs = await c.handle_control("pause")
        assert c._paused is True
        assert msgs[-1]["type"] == "snapshot"
        assert "Exclusive" in msgs[-1]["screen"]

    @pytest.mark.asyncio
    async def test_handle_control_resume(self) -> None:
        c = self._make()
        c._paused = True
        msgs = await c.handle_control("resume")
        assert c._paused is False
        assert "released" in msgs[-1]["screen"].lower()

    @pytest.mark.asyncio
    async def test_handle_control_step(self) -> None:
        c = self._make()
        msgs = await c.handle_control("step")
        assert msgs[-1]["type"] == "snapshot"

    @pytest.mark.asyncio
    async def test_handle_control_unknown(self) -> None:
        c = self._make()
        msgs = await c.handle_control("explode")
        assert "explode" in msgs[-1]["screen"]

    # -- Snapshot & analysis -------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_snapshot_shape(self) -> None:
        c = self._make()
        snap = await c.get_snapshot()
        assert snap["type"] == "snapshot"
        assert snap["cols"] == 80
        assert snap["rows"] == 25
        assert "ws://localhost:9999/ws" in snap["screen"]

    @pytest.mark.asyncio
    async def test_get_analysis(self) -> None:
        c = self._make()
        analysis = await c.get_analysis()
        assert "ws-sess" in analysis
        assert "ws://localhost:9999/ws" in analysis
        assert "input_mode" in analysis

    # -- Clear ---------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_clear(self) -> None:
        c = self._make()
        c._screen_buffer = "old stuff"
        msgs = await c.clear()
        assert c._screen_buffer == ""
        assert msgs[-1]["type"] == "snapshot"

    # -- Mode switching ------------------------------------------------------

    @pytest.mark.asyncio
    async def test_set_mode_open(self) -> None:
        c = self._make({"url": "ws://x", "input_mode": "hijack"})
        c._paused = True
        msgs = await c.set_mode("open")
        assert c._input_mode == "open"
        assert c._paused is False
        assert any(m["type"] == "worker_hello" for m in msgs)

    @pytest.mark.asyncio
    async def test_set_mode_hijack(self) -> None:
        c = self._make()
        msgs = await c.set_mode("hijack")
        assert c._input_mode == "hijack"
        assert any(m["type"] == "worker_hello" for m in msgs)

    @pytest.mark.asyncio
    async def test_set_mode_invalid_raises(self) -> None:
        c = self._make()
        with pytest.raises(ValueError, match="invalid mode"):
            await c.set_mode("root")

    # -- Snapshot metadata ---------------------------------------------------

    def test_snapshot_has_required_fields(self) -> None:
        c = self._make()
        c._screen_buffer = "line1\nline2"
        snap = c._snapshot()
        assert "screen_hash" in snap
        assert "cursor" in snap
        assert "ts" in snap
        assert snap["cursor_at_end"] is True
        assert "prompt_detected" in snap

    def test_hello_message(self) -> None:
        c = self._make({"url": "ws://x", "input_mode": "hijack"})
        hello = c._hello()
        assert hello["type"] == "worker_hello"
        assert hello["input_mode"] == "hijack"

    # -- build_connector integration -----------------------------------------

    def test_build_connector_websocket(self) -> None:
        c = build_connector("sid", "dn", "websocket", {"url": "ws://test"})
        assert isinstance(c, WebSocketSessionConnector)

    def test_build_connector_websocket_passes_session_id(self) -> None:
        c = build_connector("my-sid", "my-dn", "websocket", {"url": "ws://test"})
        assert c._session_id == "my-sid"

    def test_build_connector_websocket_passes_display_name(self) -> None:
        c = build_connector("my-sid", "my-dn", "websocket", {"url": "ws://test"})
        assert c._display_name == "my-dn"

    def test_known_connector_types_includes_websocket(self) -> None:
        assert "websocket" in KNOWN_CONNECTOR_TYPES

    def test_build_connector_ushell(self) -> None:
        from provide.uterm.shell.terminal._connector import UshellConnector

        c = build_connector("sid", "dn", "ushell", {})
        assert isinstance(c, UshellConnector)

    def test_build_connector_ushell_passes_session_id(self) -> None:
        c = build_connector("my-sid", "my-dn", "ushell", {})
        assert c._session_id == "my-sid"

    def test_build_connector_ushell_passes_display_name(self) -> None:
        c = build_connector("my-sid", "my-dn", "ushell", {})
        assert c._display_name == "my-dn"

    def test_known_connector_types_includes_ushell(self) -> None:
        assert "ushell" in KNOWN_CONNECTOR_TYPES

    def test_build_connector_shell(self) -> None:
        from provide.uterm.server.connectors.shell import ShellSessionConnector

        c = build_connector("my-sid", "my-dn", "shell", {})
        assert isinstance(c, ShellSessionConnector)
        assert c._session_id == "my-sid"
        assert c._display_name == "my-dn"

    def test_build_connector_telnet(self) -> None:
        from provide.uterm.server.connectors.telnet import TelnetSessionConnector

        c = build_connector("my-sid", "my-dn", "telnet", {})
        assert isinstance(c, TelnetSessionConnector)
        assert c._session_id == "my-sid"
        assert c._display_name == "my-dn"

    def test_build_connector_ssh(self) -> None:
        from provide.uterm.server.connectors.ssh import SshSessionConnector

        c = build_connector("my-sid", "my-dn", "ssh", {"insecure_no_host_check": True})
        assert isinstance(c, SshSessionConnector)
        assert c._session_id == "my-sid"
        assert c._display_name == "my-dn"

    def test_known_connector_types_includes_shell(self) -> None:
        assert "shell" in KNOWN_CONNECTOR_TYPES

    def test_known_connector_types_includes_telnet(self) -> None:
        assert "telnet" in KNOWN_CONNECTOR_TYPES

    def test_known_connector_types_includes_ssh(self) -> None:
        assert "ssh" in KNOWN_CONNECTOR_TYPES

    def test_build_connector_unknown_raises_value_error(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="bogus-type"):
            build_connector("sid", "dn", "bogus-type", {})
