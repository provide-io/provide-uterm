#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the ``uterm tunnel`` CLI subcommand."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.terminal.cli import _build_parser
from provide.terminal.cli.tunnel import (
    _cmd_tunnel,
    _create_tunnel,
    _handle_tcp_client,
    _read_token,
    _relay_tcp_to_ws,
    _relay_ws_to_tcp,
)
from provide.terminal.tunnel.protocol import CHANNEL_TCP, FLAG_EOF, TunnelFrame, decode_frame
from provide.terminal.tunnel.types import TunnelCreateResponse, TunnelTokenState


class TestTunnelArgParsing:
    def test_tunnel_subcommand_recognised(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["tunnel", "8080", "-s", "https://example.com"])
        assert args.port == 8080
        assert args.server == "https://example.com"

    def test_tunnel_requires_port(self) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["tunnel", "-s", "https://example.com"])

    def test_tunnel_requires_server(self) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["tunnel", "8080"])

    def test_tunnel_has_func(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["tunnel", "3000", "-s", "https://x.com"])
        assert hasattr(args, "func")

    def test_tunnel_all_flags(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "tunnel",
                "9090",
                "-s",
                "https://x.com",
                "--token",
                "tok123",
                "--display-name",
                "my-server",
            ]
        )
        assert args.port == 9090
        assert args.token == "tok123"
        assert args.display_name == "my-server"


class TestCreateTunnel:
    def test_success(self) -> None:
        resp = json.dumps(
            {
                "tunnel_id": "tunnel-abc",
                "ws_endpoint": "ws://x/tunnel/tunnel-abc",
                "worker_token": "wt",
                "share_url": "http://x/s/tunnel-abc?token=st",
            }
        ).encode()
        with patch("provide.terminal.cli.tunnel.urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__ = lambda s: MagicMock(read=lambda: resp)
            mock_open.return_value.__exit__ = MagicMock(return_value=False)
            result = _create_tunnel("https://example.com", "test", None, 8080)
        assert result["tunnel_id"] == "tunnel-abc"

    def test_with_token(self) -> None:
        """Line 71: Authorization header set when token provided."""
        resp = json.dumps({"tunnel_id": "t", "ws_endpoint": "ws://x", "worker_token": "w", "share_url": ""}).encode()
        with patch("provide.terminal.cli.tunnel.urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__ = lambda s: MagicMock(read=lambda: resp)
            mock_open.return_value.__exit__ = MagicMock(return_value=False)
            _create_tunnel("https://example.com", "test", "my-bearer-token", 8080)
        req_obj = mock_open.call_args[0][0]
        assert req_obj.get_header("Authorization") == "Bearer my-bearer-token"

    def test_http_error(self) -> None:
        import urllib.error

        with (
            patch("provide.terminal.cli.tunnel.urllib.request.urlopen") as mock_open,
            pytest.raises(SystemExit),
        ):
            mock_open.side_effect = urllib.error.HTTPError(
                "http://x",
                500,
                "fail",
                {},
                None,  # type: ignore[arg-type]
            )
            _create_tunnel("https://example.com", "test", None, 8080)

    def test_url_error(self) -> None:
        import urllib.error

        with (
            patch("provide.terminal.cli.tunnel.urllib.request.urlopen") as mock_open,
            pytest.raises(SystemExit),
        ):
            mock_open.side_effect = urllib.error.URLError("no host")
            _create_tunnel("https://example.com", "test", None, 8080)


class TestReadToken:
    def test_explicit_token(self) -> None:
        args = MagicMock(token="my-tok", token_file=None)
        assert _read_token(args) == "my-tok"

    def test_no_token(self) -> None:
        args = MagicMock(token=None, token_file="/nonexistent")
        assert _read_token(args) is None


class TestRelayTcpToWs:
    @pytest.mark.asyncio
    async def test_sends_data_frames(self) -> None:
        reader = AsyncMock()
        reader.read = AsyncMock(side_effect=[b"hello", b"world", b""])
        sent: list[bytes] = []
        ws_send = AsyncMock(side_effect=lambda d: sent.append(d))

        await _relay_tcp_to_ws(reader, ws_send)

        assert len(sent) == 3  # 2 data + 1 EOF
        # First two are data frames on channel 2
        assert sent[0][0] == CHANNEL_TCP
        assert sent[1][0] == CHANNEL_TCP
        # Last is EOF
        frame = decode_frame(sent[2])
        assert frame.is_eof and frame.channel == CHANNEL_TCP

    @pytest.mark.asyncio
    async def test_handles_connection_error(self) -> None:
        reader = AsyncMock()
        reader.read = AsyncMock(side_effect=ConnectionError("gone"))
        ws_send = AsyncMock()
        await _relay_tcp_to_ws(reader, ws_send)  # should not raise


class TestRelayWsToTcp:
    @pytest.mark.asyncio
    async def test_writes_to_tcp(self) -> None:
        writer = AsyncMock()
        writer.close = MagicMock()
        frames = [
            TunnelFrame(channel=CHANNEL_TCP, flags=0, payload=b"hello"),
            TunnelFrame(channel=CHANNEL_TCP, flags=FLAG_EOF, payload=b""),
        ]
        ws_recv = AsyncMock(side_effect=frames)

        await _relay_ws_to_tcp(ws_recv, writer)

        writer.write.assert_called_once_with(b"hello")
        writer.drain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_non_tcp_channel(self) -> None:
        writer = AsyncMock()
        writer.close = MagicMock()
        frames = [
            TunnelFrame(channel=0x01, flags=0, payload=b"terminal data"),
            TunnelFrame(channel=CHANNEL_TCP, flags=FLAG_EOF, payload=b""),
        ]
        ws_recv = AsyncMock(side_effect=frames)

        await _relay_ws_to_tcp(ws_recv, writer)

        writer.write.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_connection_error(self) -> None:
        writer = AsyncMock()
        writer.close = MagicMock()
        ws_recv = AsyncMock(side_effect=ConnectionError("broken"))
        await _relay_ws_to_tcp(ws_recv, writer)  # should not raise


class TestHandleTcpClient:
    @pytest.mark.asyncio
    async def test_bridges_reader_writer(self) -> None:
        reader = AsyncMock()
        reader.read = AsyncMock(side_effect=[b"data", b""])
        writer = AsyncMock()
        writer.close = MagicMock()
        sent: list[bytes] = []
        ws_send = AsyncMock(side_effect=lambda d: sent.append(d))
        frames = [TunnelFrame(channel=CHANNEL_TCP, flags=FLAG_EOF, payload=b"")]
        ws_recv = AsyncMock(side_effect=frames)

        await _handle_tcp_client(reader, writer, ws_send, ws_recv)
        assert len(sent) >= 1


class TestCmdTunnel:
    def test_missing_ws_endpoint(self) -> None:
        args = MagicMock(
            server="https://example.com",
            port=8080,
            display_name=None,
            token=None,
            token_file="/nonexistent",
        )
        with (
            patch(
                "provide.terminal.cli.tunnel._create_tunnel",
                return_value={"ws_endpoint": "", "worker_token": "", "share_url": ""},
            ),
            pytest.raises(SystemExit),
        ):
            _cmd_tunnel(args)

    def test_relative_ws_endpoint(self) -> None:
        args = MagicMock(
            server="https://example.com",
            port=8080,
            display_name="test",
            token=None,
            token_file="/nonexistent",
        )
        with (
            patch(
                "provide.terminal.cli.tunnel._create_tunnel",
                return_value={
                    "ws_endpoint": "/tunnel/tunnel-abc",
                    "worker_token": "wt",
                    "share_url": "https://example.com/s/tunnel-abc",
                },
            ),
            patch("provide.terminal.cli.tunnel.asyncio.run") as mock_run,
        ):
            _cmd_tunnel(args)
            mock_run.assert_called_once()

    def test_default_display_name(self) -> None:
        args = MagicMock(
            server="https://example.com",
            port=3000,
            display_name=None,
            token=None,
            token_file="/nonexistent",
        )
        with (
            patch("provide.terminal.cli.tunnel._create_tunnel") as mock_create,
            patch("provide.terminal.cli.tunnel.asyncio.run"),
        ):
            mock_create.return_value = {"ws_endpoint": "ws://x/tunnel/t", "worker_token": "", "share_url": ""}
            _cmd_tunnel(args)
            assert mock_create.call_args[0][1] == "tcp:3000"

    def test_keyboard_interrupt(self) -> None:
        args = MagicMock(
            server="https://example.com",
            port=8080,
            display_name=None,
            token=None,
            token_file="/nonexistent",
        )
        with (
            patch(
                "provide.terminal.cli.tunnel._create_tunnel",
                return_value={
                    "ws_endpoint": "ws://x/tunnel/t",
                    "worker_token": "",
                    "share_url": "",
                },
            ),
            patch("provide.terminal.cli.tunnel.asyncio.run", side_effect=KeyboardInterrupt),
        ):
            _cmd_tunnel(args)  # should not raise


class TestReadTokenEdge:
    def test_token_file_fallback_with_bearer(self, tmp_path: object) -> None:
        """Line 71: token provided → auth header set."""
        args = MagicMock(token="bearer-123", token_file="")
        assert _read_token(args) == "bearer-123"


class TestReadTokenFromFile:
    def test_reads_from_file(self, tmp_path: object) -> None:
        token_file = tmp_path / "token"
        token_file.write_text("file-token-123\n")
        args = MagicMock(token=None, token_file=str(token_file))
        assert _read_token(args) == "file-token-123"


class TestTypes:
    def test_tunnel_token_state_fields(self) -> None:
        state: TunnelTokenState = {
            "worker_token": "w",
            "share_token": "s",
            "control_token": "c",
            "created_at": 1.0,
            "expires_at": 2.0,
            "issued_ip": None,
            "tunnel_type": "terminal",
        }
        assert state["expires_at"] > state["created_at"]

    def test_tunnel_create_response_fields(self) -> None:
        resp: TunnelCreateResponse = {
            "tunnel_id": "t-1",
            "display_name": "test",
            "tunnel_type": "tcp",
            "ws_endpoint": "ws://x",
            "worker_token": "w",
            "share_url": "http://x/s/t-1",
            "control_url": "http://x/app/operator/t-1",
            "expires_at": 9999.0,
        }
        assert resp["tunnel_id"].startswith("t-")
