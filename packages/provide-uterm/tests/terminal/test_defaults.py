#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Pin the values of TerminalDefaults constants so mutations are caught immediately."""

from __future__ import annotations

from pathlib import Path

from provide.uterm.defaults import TerminalDefaults


class TestTerminalDefaults:
    def test_telnet_host(self) -> None:
        assert TerminalDefaults.TELNET_HOST == "127.0.0.1"

    def test_telnet_port(self) -> None:
        assert TerminalDefaults.TELNET_PORT == 2102

    def test_ssh_port(self) -> None:
        assert TerminalDefaults.SSH_PORT == 2222

    def test_gateway_telnet_port(self) -> None:
        assert TerminalDefaults.GATEWAY_TELNET_PORT == 2112

    def test_gateway_ssh_port(self) -> None:
        assert TerminalDefaults.GATEWAY_SSH_PORT == 2222

    def test_bind_all(self) -> None:
        assert TerminalDefaults.BIND_ALL == "0.0.0.0"

    def test_proxy_port(self) -> None:
        assert TerminalDefaults.PROXY_PORT == 8765

    def test_proxy_ws_path(self) -> None:
        assert TerminalDefaults.PROXY_WS_PATH == "/ws/terminal"

    def test_server_host(self) -> None:
        assert TerminalDefaults.SERVER_HOST == "127.0.0.1"

    def test_server_port(self) -> None:
        assert TerminalDefaults.SERVER_PORT == 8780

    def test_telnet_remote_port(self) -> None:
        assert TerminalDefaults.TELNET_REMOTE_PORT == 23

    def test_ssh_remote_port(self) -> None:
        assert TerminalDefaults.SSH_REMOTE_PORT == 22

    def test_token_file_returns_path(self) -> None:
        assert isinstance(TerminalDefaults.token_file(), Path)

    def test_token_file_path_value(self) -> None:
        assert TerminalDefaults.token_file() == Path.home() / ".uterm" / "session_token"

    def test_token_file_consistent(self) -> None:
        assert TerminalDefaults.token_file() == TerminalDefaults.token_file()

    def test_string_types(self) -> None:
        assert isinstance(TerminalDefaults.TELNET_HOST, str)
        assert isinstance(TerminalDefaults.BIND_ALL, str)
        assert isinstance(TerminalDefaults.PROXY_WS_PATH, str)
        assert isinstance(TerminalDefaults.SERVER_HOST, str)

    def test_int_types(self) -> None:
        assert isinstance(TerminalDefaults.TELNET_PORT, int)
        assert isinstance(TerminalDefaults.SSH_PORT, int)
        assert isinstance(TerminalDefaults.GATEWAY_TELNET_PORT, int)
        assert isinstance(TerminalDefaults.GATEWAY_SSH_PORT, int)
        assert isinstance(TerminalDefaults.PROXY_PORT, int)
        assert isinstance(TerminalDefaults.SERVER_PORT, int)
        assert isinstance(TerminalDefaults.TELNET_REMOTE_PORT, int)
        assert isinstance(TerminalDefaults.SSH_REMOTE_PORT, int)
