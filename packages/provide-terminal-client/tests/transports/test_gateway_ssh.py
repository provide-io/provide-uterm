#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.gateway — SshWsGateway and SSH pump helpers."""

from __future__ import annotations

import pytest
from provide.terminal.control_channel import encode_control
from provide.terminal.gateway import SshWsGateway, _ssh_to_ws, _ws_to_ssh
from provide.terminal.gateway._gateway import _make_no_auth_server_class

# ---------------------------------------------------------------------------
# SshWsGateway — init
# ---------------------------------------------------------------------------


class TestSshWsGatewayInit:
    def test_init_requires_asyncssh(self) -> None:
        """SshWsGateway can be created when asyncssh is available."""
        import asyncssh  # noqa: F401

        gw = SshWsGateway("wss://example.com/ws")
        assert gw._ws_url == "wss://example.com/ws"
        assert gw._server_key is None

    def test_init_with_server_key(self, tmp_path) -> None:
        key_path = tmp_path / "key.pem"
        key_path.write_text("dummy")
        gw = SshWsGateway("wss://example.com/ws", server_key=str(key_path))
        assert gw._server_key == str(key_path)


# ---------------------------------------------------------------------------
# SshWsGateway — start
# ---------------------------------------------------------------------------


class TestSshWsGatewayStart:
    async def test_start_ephemeral_key(self) -> None:
        """SshWsGateway.start() creates an asyncssh server with ephemeral key."""
        import asyncssh

        gw = SshWsGateway("wss://example.com/ws")
        srv = await gw.start("127.0.0.1", 0)
        assert isinstance(srv, asyncssh.SSHAcceptor)
        try:
            pass
        finally:
            srv.close()
            await srv.wait_closed()

    async def test_start_with_file_key(self, tmp_path) -> None:
        """SshWsGateway.start() loads a host key from file when provided."""
        import asyncssh

        key = asyncssh.generate_private_key("ssh-ed25519")
        key_path = tmp_path / "host_key"
        key_path.write_bytes(key.export_private_key())

        gw = SshWsGateway("wss://example.com/ws", server_key=str(key_path))
        srv = await gw.start("127.0.0.1", 0)
        assert isinstance(srv, asyncssh.SSHAcceptor)
        try:
            pass
        finally:
            srv.close()
            await srv.wait_closed()

    async def test_start_missing_key_file_raises(self, tmp_path) -> None:
        """SshWsGateway.start() raises FileNotFoundError for a missing key path."""
        gw = SshWsGateway("wss://example.com/ws", server_key=str(tmp_path / "no_such_key"))
        with pytest.raises(FileNotFoundError, match="SSH host key not found"):
            await gw.start("127.0.0.1", 0)

    async def test_start_key_path_is_directory_raises(self, tmp_path) -> None:
        """SshWsGateway.start() raises ValueError when key path is a directory."""
        gw = SshWsGateway("wss://example.com/ws", server_key=str(tmp_path))
        with pytest.raises(ValueError, match="not a file"):
            await gw.start("127.0.0.1", 0)


# ---------------------------------------------------------------------------
# _ssh_to_ws pump helper
# ---------------------------------------------------------------------------


class TestSshToWs:
    async def test_ssh_to_ws_str_data(self) -> None:
        sent = []

        class _MockWs:
            async def send(self, data: object) -> None:
                sent.append(data)

        class _MockStdin:
            def __init__(self) -> None:
                self._data = ["hello", ""]

            async def read(self, n: int) -> str:
                return self._data.pop(0)

        class _MockProcess:
            stdin = _MockStdin()

        await _ssh_to_ws(_MockProcess(), _MockWs())
        assert sent == ["hello"]

    async def test_ssh_to_ws_bytes_data(self) -> None:
        sent = []

        class _MockWs:
            async def send(self, data: object) -> None:
                sent.append(data)

        class _MockStdin:
            def __init__(self) -> None:
                self._data = [b"hello", b""]

            async def read(self, n: int) -> bytes:
                return self._data.pop(0)

        class _MockProcess:
            stdin = _MockStdin()

        await _ssh_to_ws(_MockProcess(), _MockWs())
        assert "hello" in sent[0]

    async def test_ssh_to_ws_exception_exits(self) -> None:
        class _MockWs:
            async def send(self, data: object) -> None:
                pass

        class _MockStdin:
            async def read(self, n: int) -> bytes:
                raise RuntimeError("broken")

        class _MockProcess:
            stdin = _MockStdin()

        # Should exit cleanly without raising
        await _ssh_to_ws(_MockProcess(), _MockWs())


# ---------------------------------------------------------------------------
# _ws_to_ssh pump helper
# ---------------------------------------------------------------------------


class TestWsToSsh:
    async def test_ws_to_ssh_str_message(self) -> None:
        written = []

        class _MockStdout:
            def write(self, data: object) -> None:
                written.append(data)

        class _MockProcess:
            stdout = _MockStdout()

        async def _gen():
            yield "hello"

        await _ws_to_ssh(_gen(), _MockProcess())
        assert "hello" in written

    async def test_ws_to_ssh_bytes_message(self) -> None:
        written = []

        class _MockStdout:
            def write(self, data: object) -> None:
                written.append(data)

        class _MockProcess:
            stdout = _MockStdout()

        async def _gen():
            yield b"world"

        await _ws_to_ssh(_gen(), _MockProcess())
        assert "world" in written[0]


# ---------------------------------------------------------------------------
# _ws_to_ssh — control message coverage
# ---------------------------------------------------------------------------


class TestWsToSshControl:
    async def test_resume_ok_writes_to_stdout(self) -> None:
        """resume_ok control message triggers _write_fn (writes to SSH stdout)."""
        written: list[str] = []

        class _MockStdout:
            def write(self, data: object) -> None:
                written.append(str(data))

        class _MockProcess:
            stdout = _MockStdout()

        async def _gen():
            yield encode_control({"type": "resume_ok"})

        await _ws_to_ssh(_gen(), _MockProcess())
        assert any("Session resumed" in w for w in written)

    async def test_session_token_control_intercepted(self, tmp_path) -> None:
        """session_token control message is intercepted and not written to stdout."""
        written: list[str] = []
        token_file = tmp_path / "tok"

        class _MockStdout:
            def write(self, data: object) -> None:
                written.append(str(data))

        class _MockProcess:
            stdout = _MockStdout()

        async def _gen():
            yield encode_control({"type": "session_token", "token": "abc"})

        await _ws_to_ssh(_gen(), _MockProcess(), token_file=token_file)
        assert written == []
        assert token_file.read_text() == "abc"


# ---------------------------------------------------------------------------
# _make_no_auth_server_class — begin_auth
# ---------------------------------------------------------------------------


class TestNoAuthServerClass:
    def test_begin_auth_returns_false(self) -> None:
        """_NoAuthServer.begin_auth always returns False (allow all users)."""
        no_auth_cls = _make_no_auth_server_class()
        srv = no_auth_cls.__new__(no_auth_cls)
        assert srv.begin_auth("any_user") is False
