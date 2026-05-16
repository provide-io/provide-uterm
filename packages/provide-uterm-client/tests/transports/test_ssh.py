#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for SSHStreamReader and SSHStreamWriter (mock asyncssh process)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("asyncssh", reason="asyncssh not installed; skip SSH transport tests")

from provide.uterm.transports.ssh import SSHStreamReader, SSHStreamWriter


class MockStdin:
    def __init__(self, data: bytes | str) -> None:
        self._data = data

    async def read(self, n: int = -1) -> bytes | str:
        return self._data


class MockStdout:
    def __init__(self) -> None:
        self.written: bytearray = bytearray()

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        pass


from typing import TYPE_CHECKING, cast

import asyncssh


class MockProcess:
    def __init__(self, stdin_data: bytes | str = b"") -> None:
        self.stdin = MockStdin(stdin_data)
        self.stdout = MockStdout()
        self._exited = False
        self._closed = False

    def exit(self, code: int) -> None:
        self._exited = True

    def close(self) -> None:
        self._closed = True

    def get_extra_info(self, name: str) -> object:
        if name == "peername":
            return ("127.0.0.1", 12345)
        return None


class TestSSHStreamReader:
    async def test_read_bytes(self) -> None:
        proc = MockProcess(stdin_data=b"hello")
        reader = SSHStreamReader(cast("asyncssh.SSHServerProcess[bytes]", proc))
        data = await reader.read(5)
        assert data == b"hello"

    async def test_read_str_encodes_latin1(self) -> None:
        proc = MockProcess(stdin_data="hello")
        reader = SSHStreamReader(cast("asyncssh.SSHServerProcess[bytes]", proc))
        data = await reader.read(5)
        assert data == b"hello"

    async def test_read_on_error_returns_empty(self) -> None:
        import asyncssh

        proc = MockProcess()
        proc.stdin = MagicMock()
        proc.stdin.read = AsyncMock(side_effect=asyncssh.Error(1, "test", "en"))
        reader = SSHStreamReader(cast("asyncssh.SSHServerProcess[bytes]", proc))
        data = await reader.read(5)
        assert data == b""


class TestSSHStreamWriter:
    def test_write_passes_bytes(self) -> None:
        proc = MockProcess()
        writer = SSHStreamWriter(cast("asyncssh.SSHServerProcess[bytes]", proc))
        writer.write(b"test data")
        assert bytes(proc.stdout.written) == b"test data"

    def test_write_after_close_is_noop(self) -> None:
        proc = MockProcess()
        writer = SSHStreamWriter(cast("asyncssh.SSHServerProcess[bytes]", proc))
        writer.close()
        writer.write(b"ignored")
        assert bytes(proc.stdout.written) == b""

    async def test_drain_flushes(self) -> None:
        proc = MockProcess()
        writer = SSHStreamWriter(cast("asyncssh.SSHServerProcess[bytes]", proc))
        writer.write(b"data")
        await writer.drain()  # should not raise

    def test_get_extra_info_peername(self) -> None:
        proc = MockProcess()
        writer = SSHStreamWriter(cast("asyncssh.SSHServerProcess[bytes]", proc))
        peer = writer.get_extra_info("peername")
        assert peer == ("127.0.0.1", 12345)

    def test_get_extra_info_unknown(self) -> None:
        proc = MockProcess()
        writer = SSHStreamWriter(cast("asyncssh.SSHServerProcess[bytes]", proc))
        assert writer.get_extra_info("unknown", "default") == "default"

    def test_close_exits_process(self) -> None:
        proc = MockProcess()
        writer = SSHStreamWriter(cast("asyncssh.SSHServerProcess[bytes]", proc))
        writer.close()
        assert proc._exited


class TestSSHStreamWriterEdgeCases:
    def test_write_with_os_error_calls_close(self) -> None:
        proc = MockProcess()
        proc.stdout = MagicMock()
        proc.stdout.write = MagicMock(side_effect=OSError("broken pipe"))
        writer = SSHStreamWriter(cast("asyncssh.SSHServerProcess[bytes]", proc))
        writer.write(b"data")
        assert writer._closed  # error → close called

    async def test_drain_when_closed_noop(self) -> None:
        proc = MockProcess()
        writer = SSHStreamWriter(cast("asyncssh.SSHServerProcess[bytes]", proc))
        writer._closed = True
        await writer.drain()  # should not raise or call stdout.drain

    async def test_drain_with_os_error_calls_close(self) -> None:
        proc = MockProcess()
        proc.stdout.drain = AsyncMock(side_effect=OSError("broken"))  # type: ignore[assignment]
        writer = SSHStreamWriter(cast("asyncssh.SSHServerProcess[bytes]", proc))
        await writer.drain()
        assert writer._closed

    def test_get_extra_info_no_peername(self) -> None:
        proc = MockProcess()
        proc.get_extra_info = MagicMock(return_value=None)  # type: ignore[assignment]
        writer = SSHStreamWriter(cast("asyncssh.SSHServerProcess[bytes]", proc))
        result = writer.get_extra_info("peername")
        assert result is None


class TestTerminalSSHServer:
    def test_connection_made_increments_count(self) -> None:
        from provide.uterm.transports.ssh import TerminalSSHServer

        ip_connections: dict = {}
        server = TerminalSSHServer(ip_connections, max_connections_per_ip=5)
        conn = MagicMock()
        conn.get_extra_info = MagicMock(return_value=("127.0.0.1", 12345))
        server.connection_made(conn)
        assert ip_connections.get("127.0.0.1", 0) >= 1

    def test_connection_made_rate_limit(self) -> None:
        from provide.uterm.transports.ssh import TerminalSSHServer

        ip_connections: dict = {"10.0.0.1": 10}  # over limit
        server = TerminalSSHServer(ip_connections, max_connections_per_ip=5)
        conn = MagicMock()
        conn.get_extra_info = MagicMock(return_value=("10.0.0.1", 5678))
        server.connection_made(conn)
        conn.close.assert_called_once()

    def test_connection_made_no_peer(self) -> None:
        from provide.uterm.transports.ssh import TerminalSSHServer

        ip_connections: dict = {}
        server = TerminalSSHServer(ip_connections, max_connections_per_ip=5)
        conn = MagicMock()
        conn.get_extra_info = MagicMock(return_value=None)
        # Should not raise even with no peer info
        server.connection_made(conn)

    def test_connection_lost_decrements_count(self) -> None:
        from provide.uterm.transports.ssh import TerminalSSHServer

        ip_connections: dict = {"127.0.0.1": 2}
        server = TerminalSSHServer(ip_connections, max_connections_per_ip=5)
        server._peer_ip = "127.0.0.1"
        server.connection_lost(None)
        assert ip_connections.get("127.0.0.1") == 1

    def test_connection_lost_removes_zero_count(self) -> None:
        from provide.uterm.transports.ssh import TerminalSSHServer

        ip_connections: dict = {"127.0.0.1": 1}
        server = TerminalSSHServer(ip_connections, max_connections_per_ip=5)
        server._peer_ip = "127.0.0.1"
        server.connection_lost(None)
        assert "127.0.0.1" not in ip_connections

    def test_connection_lost_unknown_ip_noop(self) -> None:
        from provide.uterm.transports.ssh import TerminalSSHServer

        ip_connections: dict = {}
        server = TerminalSSHServer(ip_connections, max_connections_per_ip=5)
        server._peer_ip = "unknown_ip"
        server.connection_lost(None)  # should not raise

    def test_auth_methods(self) -> None:
        from provide.uterm.transports.ssh import TerminalSSHServer

        server = TerminalSSHServer({}, max_connections_per_ip=5)
        # The gateway-style SSH server is intentionally permissive — it relays
        # to the upstream WS, which performs real authentication. Both
        # password and public-key validators should accept any credential.
        assert server.begin_auth("user") is True
        assert server.password_auth_supported() is True
        assert server.validate_password("user", "pass") is True
        assert server.public_key_auth_supported() is True
        assert server.validate_public_key("user", MagicMock()) is True


class TestSSHPerInstanceIsolation:
    """Regression: fix 5 — two server instances must not share connection counts."""

    def test_two_factories_have_independent_counts(self) -> None:
        """Regression: _make_ssh_server_factory creates isolated ip_connections per call."""
        from provide.uterm.transports.ssh import _make_ssh_server_factory

        ip_a: dict = {}
        ip_b: dict = {}
        factory_a = _make_ssh_server_factory(ip_a, max_connections_per_ip=5)
        factory_b = _make_ssh_server_factory(ip_b, max_connections_per_ip=5)

        if TYPE_CHECKING:
            from collections.abc import Callable
            from typing import Any

            factory_a_call = cast("Callable[[], Any]", factory_a)
            factory_b_call = cast("Callable[[], Any]", factory_b)
        else:
            factory_a_call = factory_a
            factory_b_call = factory_b

        server_a = factory_a_call()
        conn_a = MagicMock()
        conn_a.get_extra_info = MagicMock(return_value=("1.2.3.4", 1000))
        server_a.connection_made(conn_a)

        # server_a's connection must appear in ip_a but NOT in ip_b
        assert ip_a.get("1.2.3.4", 0) >= 1
        assert ip_b.get("1.2.3.4", 0) == 0

        # server_b should independently track its own connections
        server_b = factory_b_call()
        conn_b = MagicMock()
        conn_b.get_extra_info = MagicMock(return_value=("5.6.7.8", 2000))
        server_b.connection_made(conn_b)
        assert ip_b.get("5.6.7.8", 0) >= 1
        assert ip_a.get("5.6.7.8", 0) == 0

    def test_rate_limit_applies_per_instance(self) -> None:
        """Regression: per-IP limit is scoped to each server instance."""
        from provide.uterm.transports.ssh import _make_ssh_server_factory

        ip_a: dict = {"10.0.0.1": 3}  # 3 connections in server A
        ip_b: dict = {}  # 0 connections in server B
        factory_a = _make_ssh_server_factory(ip_a, max_connections_per_ip=3)
        factory_b = _make_ssh_server_factory(ip_b, max_connections_per_ip=3)

        if TYPE_CHECKING:
            from collections.abc import Callable
            from typing import Any

            factory_a_call = cast("Callable[[], Any]", factory_a)
            factory_b_call = cast("Callable[[], Any]", factory_b)
        else:
            factory_a_call = factory_a
            factory_b_call = factory_b

        server_a = factory_a_call()
        conn_a = MagicMock()
        conn_a.get_extra_info = MagicMock(return_value=("10.0.0.1", 9999))
        server_a.connection_made(conn_a)
        # server_a should reject (at limit)
        conn_a.close.assert_called_once()

        server_b = factory_b_call()
        conn_b = MagicMock()
        conn_b.get_extra_info = MagicMock(return_value=("10.0.0.1", 9999))
        server_b.connection_made(conn_b)
        # server_b should accept (not at limit in its own dict)
        conn_b.close.assert_not_called()


class TestGetOrCreateHostKey:
    def test_creates_new_key(self, tmp_path) -> None:
        from provide.uterm.transports.ssh import _get_or_create_host_key

        key = _get_or_create_host_key(tmp_path)
        assert key is not None
        assert (tmp_path / "ssh_host_key").exists()

    def test_loads_existing_key(self, tmp_path) -> None:
        import asyncssh

        from provide.uterm.transports.ssh import _get_or_create_host_key

        existing_key = asyncssh.generate_private_key("ssh-ed25519")
        (tmp_path / "ssh_host_key").write_bytes(existing_key.export_private_key())

        loaded = _get_or_create_host_key(tmp_path)
        assert loaded is not None

    def test_regenerates_corrupted_key(self, tmp_path) -> None:
        from provide.uterm.transports.ssh import _get_or_create_host_key

        (tmp_path / "ssh_host_key").write_bytes(b"not a valid key")
        key = _get_or_create_host_key(tmp_path)
        assert key is not None


class TestStartSshServer:
    async def test_start_ssh_server_basic(self, tmp_path) -> None:
        from provide.uterm.transports.ssh import (
            SSHStreamReader,
            SSHStreamWriter,
            start_ssh_server,
        )

        seen: dict[str, object] = {}

        async def _handler(reader: object, writer: object) -> None:
            seen["reader"] = reader
            seen["writer"] = writer

        async def _create_server(server_class, host, port, **kwargs):
            assert host == "127.0.0.1"
            assert port == 0
            assert kwargs["server_host_keys"]
            assert kwargs["encoding"] is None
            process_factory = kwargs["process_factory"]
            proc = MockProcess()
            await process_factory(cast("asyncssh.SSHServerProcess[bytes]", proc))
            return MagicMock()

        with patch("provide.uterm.transports.ssh.asyncssh.create_server", side_effect=_create_server) as mock_create:
            server = await start_ssh_server(_handler, host="127.0.0.1", port=0, host_key_path=tmp_path)
        assert server is not None
        assert isinstance(seen["reader"], SSHStreamReader)
        assert isinstance(seen["writer"], SSHStreamWriter)
        mock_create.assert_called_once()

    async def test_start_ssh_server_uses_injected_factories(self, tmp_path) -> None:
        from provide.uterm.transports.ssh import start_ssh_server

        seen: dict[str, object] = {}

        class DummyReader:
            def __init__(self, process) -> None:
                seen["reader_process"] = process

        class DummyWriter:
            def __init__(self, process) -> None:
                seen["writer_process"] = process

        async def _handler(reader: object, writer: object) -> None:
            seen["handler_reader"] = reader
            seen["handler_writer"] = writer

        async def _create_server(server_class, host, port, **kwargs):
            seen["server_class"] = server_class
            seen["host"] = host
            seen["port"] = port
            process_factory = kwargs["process_factory"]
            proc = MockProcess()
            await process_factory(cast("asyncssh.SSHServerProcess[bytes]", proc))
            return MagicMock()

        with patch("provide.uterm.transports.ssh.asyncssh.create_server", side_effect=_create_server):
            server = await start_ssh_server(
                _handler,
                host="127.0.0.1",
                port=0,
                host_key_path=tmp_path,
                reader_factory=DummyReader,
                writer_factory=DummyWriter,
            )

        assert server is not None
        assert seen["host"] == "127.0.0.1"
        assert seen["port"] == 0
        assert isinstance(seen["handler_reader"], DummyReader)
        assert isinstance(seen["handler_writer"], DummyWriter)
        assert seen["reader_process"] is not None
        assert seen["writer_process"] is not None


class TestGetOrCreateHostKeySaveFailure:
    def test_save_failure_logs_error_and_returns_key(self, tmp_path) -> None:
        """When saving the generated key fails, the key is still returned."""
        import stat

        from provide.uterm.transports.ssh import _get_or_create_host_key

        # Make the directory read-only so key_path.write_bytes() fails
        tmp_path.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            key = _get_or_create_host_key(tmp_path)
            assert key is not None
        finally:
            tmp_path.chmod(stat.S_IRWXU)


class TestSSHStreamReaderUnknownType:
    async def test_read_unknown_type_returns_empty_bytes(self) -> None:
        """Line 57: data is not str/bytes/bytearray → return b''."""
        proc = MockProcess()
        proc.stdin.read = AsyncMock(return_value=42)  # int — not str/bytes/bytearray
        reader = SSHStreamReader(cast("asyncssh.SSHServerProcess[bytes]", proc))
        data = await reader.read(1)
        assert data == b""


class TestSSHStreamWriterDoubleClose:
    def test_close_when_already_closed_is_noop(self) -> None:
        """Line 87->exit: _closed is already True → skip close body."""
        proc = MockProcess()
        writer = SSHStreamWriter(cast("asyncssh.SSHServerProcess[bytes]", proc))
        writer._closed = True
        writer.close()  # Should be a no-op, not call exit/close again
        assert writer._closed
