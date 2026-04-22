#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.gateway._ssh_gateway.SshWsGateway."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from provide.terminal.gateway._ssh_gateway import SshWsGateway


class TestSshWsGatewayInit:
    def test_default_init(self) -> None:
        gw = SshWsGateway("ws://test")
        assert gw._ws_url == "ws://test"
        assert gw._server_key is None
        assert gw._color_mode == "passthrough"

    def test_init_with_options(self) -> None:
        gw = SshWsGateway("ws://test", server_key="/tmp/key", color_mode="16")  # noqa: S108
        assert gw._server_key == "/tmp/key"  # noqa: S108
        assert gw._color_mode == "16"


class TestSshWsGatewayStart:
    async def test_start_with_generated_key(self) -> None:
        gw = SshWsGateway("ws://test")

        mock_key = MagicMock()
        mock_server = AsyncMock()

        with (
            patch("asyncssh.generate_private_key", return_value=mock_key) as gen_key,
            patch("asyncssh.create_server", new_callable=AsyncMock, return_value=mock_server) as create_srv,
        ):
            result = await gw.start("127.0.0.1", 0)

        assert result == mock_server
        gen_key.assert_called_once_with("ssh-ed25519")
        create_srv.assert_called_once()

    async def test_start_with_server_key_file(self, tmp_path: Path) -> None:
        key_file = tmp_path / "host_key"
        key_file.write_text("fake key content")

        gw = SshWsGateway("ws://test", server_key=key_file)

        mock_key = MagicMock()
        mock_server = AsyncMock()

        with (
            patch("asyncssh.read_private_key", return_value=mock_key) as read_key,
            patch("asyncssh.create_server", new_callable=AsyncMock, return_value=mock_server) as create_srv,
        ):
            result = await gw.start("127.0.0.1", 0)

        assert result == mock_server
        read_key.assert_called_once_with(str(key_file))
        create_srv.assert_called_once()

    async def test_start_key_not_found(self, tmp_path: Path) -> None:
        gw = SshWsGateway("ws://test", server_key=tmp_path / "nonexistent")
        try:
            await gw.start("127.0.0.1", 0)
            raise AssertionError("should have raised FileNotFoundError")
        except FileNotFoundError as exc:
            assert "SSH host key not found" in str(exc)

    async def test_start_key_is_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "keydir"
        d.mkdir()
        gw = SshWsGateway("ws://test", server_key=d)
        try:
            await gw.start("127.0.0.1", 0)
            raise AssertionError("should have raised ValueError")
        except ValueError as exc:
            assert "not a file" in str(exc)

    async def test_start_passes_process_factory(self) -> None:
        gw = SshWsGateway("ws://test")

        mock_key = MagicMock()
        mock_server = AsyncMock()

        with (
            patch("asyncssh.generate_private_key", return_value=mock_key),
            patch("asyncssh.create_server", new_callable=AsyncMock, return_value=mock_server) as create_srv,
        ):
            await gw.start("127.0.0.1", 2222)

        call_kwargs = create_srv.call_args
        assert call_kwargs[1]["process_factory"] is not None
        assert call_kwargs[0][1] == "127.0.0.1"
        assert call_kwargs[0][2] == 2222


class TestSshWsGatewayStartWithStringKey:
    async def test_start_with_string_server_key(self, tmp_path: Path) -> None:
        key_file = tmp_path / "host_key"
        key_file.write_text("fake key content")

        gw = SshWsGateway("ws://test", server_key=str(key_file))

        mock_key = MagicMock()
        mock_server = AsyncMock()

        with (
            patch("asyncssh.read_private_key", return_value=mock_key),
            patch("asyncssh.create_server", new_callable=AsyncMock, return_value=mock_server),
        ):
            result = await gw.start("127.0.0.1", 0)

        assert result == mock_server
