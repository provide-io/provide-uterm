#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for TelnetSessionConnector and SshSessionConnector."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# TelnetSessionConnector
# ---------------------------------------------------------------------------


def _make_mock_transport(*, connected: bool = True, recv_data: bytes = b"") -> MagicMock:
    t = MagicMock()
    t.is_connected.return_value = connected
    t.connect = AsyncMock()
    t.disconnect = AsyncMock()
    t.send = AsyncMock()
    t.receive = AsyncMock(return_value=recv_data)
    return t


class TestTelnetSessionConnector:
    def _make(self, config: dict[str, Any] | None = None, transport: MagicMock | None = None) -> Any:
        from provide.uterm.server.connectors.telnet import TelnetSessionConnector

        c = TelnetSessionConnector("sess2", "Test Telnet", config or {"host": "127.0.0.1", "port": 2323})
        if transport is not None:
            c._transport = transport
        return c

    @pytest.mark.asyncio
    async def test_start_stop(self) -> None:
        t = _make_mock_transport()
        c = self._make(transport=t)
        await c.start()
        t.connect.assert_awaited_once_with("127.0.0.1", 2323)
        assert c.is_connected()
        await c.stop()
        t.disconnect.assert_awaited_once()
        assert not c.is_connected()

    @pytest.mark.asyncio
    async def test_is_connected_delegates_to_transport(self) -> None:
        t = _make_mock_transport(connected=False)
        c = self._make(transport=t)
        # Even after start() sets _connected=True, transport says False
        c._connected = True
        assert not c.is_connected()

    @pytest.mark.asyncio
    async def test_poll_messages_no_data(self) -> None:
        t = _make_mock_transport(recv_data=b"")
        c = self._make(transport=t)
        c._connected = True
        msgs = await c.poll_messages()
        assert msgs == []

    @pytest.mark.asyncio
    async def test_poll_messages_with_data(self) -> None:
        t = _make_mock_transport(recv_data=b"hello\r\n")
        c = self._make(transport=t)
        c._connected = True
        msgs = await c.poll_messages()
        assert any(m["type"] == "term" for m in msgs)
        assert any(m["type"] == "snapshot" for m in msgs)
        assert c._received_bytes == 7

    @pytest.mark.asyncio
    async def test_poll_messages_not_connected(self) -> None:
        t = _make_mock_transport(connected=False)
        c = self._make(transport=t)
        c._connected = False
        assert await c.poll_messages() == []

    @pytest.mark.asyncio
    async def test_get_snapshot_shape(self) -> None:
        c = self._make()
        snap = await c.get_snapshot()
        assert snap["type"] == "snapshot"
        assert snap["cols"] == 80
        assert "127.0.0.1" in snap["screen"]

    @pytest.mark.asyncio
    async def test_get_analysis(self) -> None:
        c = self._make()
        analysis = await c.get_analysis()
        assert "sess2" in analysis
        assert "host" in analysis

    @pytest.mark.asyncio
    async def test_handle_input(self) -> None:
        t = _make_mock_transport()
        c = self._make(transport=t)
        c._connected = True
        msgs = await c.handle_input("hello")
        t.send.assert_awaited_once()
        assert msgs[-1]["type"] == "snapshot"

    @pytest.mark.asyncio
    async def test_handle_input_not_connected(self) -> None:
        t = _make_mock_transport(connected=False)
        c = self._make(transport=t)
        c._connected = False
        msgs = await c.handle_input("hello")
        t.send.assert_not_awaited()
        assert msgs[-1]["type"] == "snapshot"

    @pytest.mark.asyncio
    async def test_handle_control_pause(self) -> None:
        c = self._make()
        msgs = await c.handle_control("pause")
        assert c._paused is True
        assert msgs[-1]["type"] == "snapshot"
        assert "Paused" in msgs[-1]["screen"]

    @pytest.mark.asyncio
    async def test_handle_control_resume(self) -> None:
        c = self._make()
        c._paused = True
        msgs = await c.handle_control("resume")
        assert c._paused is False
        assert "Live" in msgs[-1]["screen"]

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

    @pytest.mark.asyncio
    async def test_clear(self) -> None:
        c = self._make()
        c._screen_buffer = "some old content"
        msgs = await c.clear()
        assert c._screen_buffer == ""
        assert msgs[-1]["type"] == "snapshot"

    @pytest.mark.asyncio
    async def test_set_mode_open(self) -> None:
        c = self._make({"host": "h", "port": 23, "input_mode": "hijack"})
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


# ---------------------------------------------------------------------------
# SshSessionConnector
# ---------------------------------------------------------------------------


def _make_ssh_connector(config: dict[str, Any] | None = None) -> Any:
    pytest.importorskip("asyncssh", reason="asyncssh not installed")
    from provide.uterm.server.connectors.ssh import SshSessionConnector

    return SshSessionConnector("sess3", "Test SSH", config or {"host": "localhost", "insecure_no_host_check": True})


def _attach_mock_ssh(connector: Any) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Attach mock SSH conn/process/stdin/stdout to an already-constructed connector."""
    mock_stdout = MagicMock()
    mock_stdin = MagicMock()
    mock_stdin.drain = AsyncMock()
    mock_conn = MagicMock()
    mock_conn.close = MagicMock()
    mock_conn.wait_closed = AsyncMock()
    connector._conn = mock_conn
    connector._stdin = mock_stdin
    connector._stdout = mock_stdout
    connector._connected = True
    return mock_conn, mock_stdin, mock_stdout


class TestSshSessionConnector:
    def test_client_keys_list(self) -> None:
        pytest.importorskip("asyncssh")
        from provide.uterm.server.connectors.ssh import SshSessionConnector

        c = SshSessionConnector(
            "s", "S", {"host": "h", "insecure_no_host_check": True, "client_keys": ["key1", None, "key2"]}
        )
        assert "key1" in c._client_keys
        assert "key2" in c._client_keys
        assert None not in c._client_keys

    def test_client_keys_scalar(self) -> None:
        pytest.importorskip("asyncssh")
        from provide.uterm.server.connectors.ssh import SshSessionConnector

        c = SshSessionConnector("s", "S", {"host": "h", "insecure_no_host_check": True, "client_keys": "mykey"})
        assert "mykey" in c._client_keys

    def test_client_key_path(self) -> None:
        pytest.importorskip("asyncssh")
        from provide.uterm.server.connectors.ssh import SshSessionConnector

        c = SshSessionConnector("s", "S", {"host": "h", "insecure_no_host_check": True, "client_key_path": "/tmp/id"})  # noqa: S108,RUF100
        assert "/tmp/id" in c._client_keys

    def test_client_key_str(self) -> None:
        pytest.importorskip("asyncssh")
        from provide.uterm.server.connectors.ssh import SshSessionConnector

        c = SshSessionConnector("s", "S", {"host": "h", "insecure_no_host_check": True, "client_key": "inline_key"})
        assert "inline_key" in c._client_keys

    def test_client_key_data_bytes(self) -> None:
        pytest.importorskip("asyncssh")
        import asyncssh

        from provide.uterm.server.connectors.ssh import SshSessionConnector

        sentinel = object()
        with patch.object(asyncssh, "import_private_key", return_value=sentinel) as mock_import:
            c = SshSessionConnector(
                "s", "S", {"host": "h", "insecure_no_host_check": True, "client_key_data": b"PEM DATA"}
            )
            mock_import.assert_called_once_with(b"PEM DATA")
            assert sentinel in c._client_keys

    def test_client_key_data_str(self) -> None:
        pytest.importorskip("asyncssh")
        import asyncssh

        from provide.uterm.server.connectors.ssh import SshSessionConnector

        sentinel = object()
        with patch.object(asyncssh, "import_private_key", return_value=sentinel) as mock_import:
            c = SshSessionConnector(
                "s", "S", {"host": "h", "insecure_no_host_check": True, "client_key_data": "PEM STRING"}
            )
            mock_import.assert_called_once_with(b"PEM STRING")
            assert sentinel in c._client_keys

    @pytest.mark.asyncio
    async def test_poll_messages_timeout(self) -> None:
        c = _make_ssh_connector()
        _, _, mock_stdout = _attach_mock_ssh(c)
        mock_stdout.read = AsyncMock(side_effect=TimeoutError)
        msgs = await c.poll_messages()
        assert msgs == []

    @pytest.mark.asyncio
    async def test_poll_messages_empty_read(self) -> None:
        c = _make_ssh_connector()
        _attach_mock_ssh(c)
        with patch("asyncio.wait_for", new=AsyncMock(return_value=b"")):
            msgs = await c.poll_messages()
        assert msgs == []

    @pytest.mark.asyncio
    async def test_poll_messages_bytes_data(self) -> None:
        c = _make_ssh_connector()
        _, _, mock_stdout = _attach_mock_ssh(c)
        with patch("asyncio.wait_for", new=AsyncMock(return_value=b"hello\r\n")):
            msgs = await c.poll_messages()
        assert any(m["type"] == "term" for m in msgs)
        assert any(m["type"] == "snapshot" for m in msgs)
        assert c._bytes_received == 7

    @pytest.mark.asyncio
    async def test_poll_messages_str_data(self) -> None:
        """str data from asyncssh is encoded as latin-1 for the payload."""
        c = _make_ssh_connector()
        _, _, mock_stdout = _attach_mock_ssh(c)
        with patch("asyncio.wait_for", new=AsyncMock(return_value="hello")):
            msgs = await c.poll_messages()
        assert any(m["type"] == "term" for m in msgs)

    @pytest.mark.asyncio
    async def test_poll_messages_not_connected(self) -> None:
        c = _make_ssh_connector()
        c._connected = False
        assert await c.poll_messages() == []

    @pytest.mark.asyncio
    async def test_handle_control_pause(self) -> None:
        c = _make_ssh_connector()
        msgs = await c.handle_control("pause")
        assert c._paused is True
        assert msgs[-1]["type"] == "snapshot"
        assert "Paused" in msgs[-1]["screen"]

    @pytest.mark.asyncio
    async def test_handle_control_resume(self) -> None:
        c = _make_ssh_connector()
        c._paused = True
        msgs = await c.handle_control("resume")
        assert c._paused is False
        assert "Live" in msgs[-1]["screen"]

    @pytest.mark.asyncio
    async def test_handle_control_step(self) -> None:
        c = _make_ssh_connector()
        msgs = await c.handle_control("step")
        assert msgs[-1]["type"] == "snapshot"
        assert "Step" in msgs[-1]["screen"]

    @pytest.mark.asyncio
    async def test_handle_control_unknown(self) -> None:
        c = _make_ssh_connector()
        msgs = await c.handle_control("explode")
        assert "explode" in msgs[-1]["screen"]

    @pytest.mark.asyncio
    async def test_clear(self) -> None:
        c = _make_ssh_connector()
        c._screen_buffer = "old content"
        msgs = await c.clear()
        assert c._screen_buffer == ""
        assert msgs[-1]["type"] == "snapshot"

    @pytest.mark.asyncio
    async def test_set_mode_open_clears_paused(self) -> None:
        c = _make_ssh_connector()
        c._input_mode = "hijack"
        c._paused = True
        msgs = await c.set_mode("open")
        assert c._input_mode == "open"
        assert c._paused is False
        assert any(m["type"] == "worker_hello" for m in msgs)

    @pytest.mark.asyncio
    async def test_set_mode_hijack(self) -> None:
        c = _make_ssh_connector()
        msgs = await c.set_mode("hijack")
        assert c._input_mode == "hijack"
        assert any(m["type"] == "worker_hello" for m in msgs)

    @pytest.mark.asyncio
    async def test_set_mode_invalid_raises(self) -> None:
        c = _make_ssh_connector()
        with pytest.raises(ValueError, match="invalid mode"):
            await c.set_mode("root")

    @pytest.mark.asyncio
    async def test_get_analysis(self) -> None:
        c = _make_ssh_connector()
        analysis = await c.get_analysis()
        assert "sess3" in analysis
        assert "host" in analysis
        assert "bytes_received" in analysis

    @pytest.mark.asyncio
    async def test_stop_cleans_up(self) -> None:
        c = _make_ssh_connector()
        mock_conn, mock_stdin, _ = _attach_mock_ssh(c)
        await c.stop()
        assert not c._connected
        assert c._conn is None
        mock_conn.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_input_no_stdin(self) -> None:
        c = _make_ssh_connector()
        c._stdin = None
        msgs = await c.handle_input("hello")
        assert msgs[-1]["type"] == "snapshot"

    @pytest.mark.asyncio
    async def test_stop_calls_stdin_write_eof_and_process_close(self) -> None:
        c = _make_ssh_connector()
        mock_conn = MagicMock()
        mock_conn.close = MagicMock()
        mock_conn.wait_closed = AsyncMock()
        mock_process = MagicMock()
        mock_process.close = MagicMock()
        mock_stdin = MagicMock()
        mock_stdin.write_eof = MagicMock()
        c._conn = mock_conn
        c._process = mock_process
        c._stdin = mock_stdin
        c._stdout = MagicMock()
        c._connected = True
        await c.stop()
        mock_stdin.write_eof.assert_called_once()
        mock_process.close.assert_called_once()
        mock_conn.close.assert_called_once()
        assert c._conn is None
        assert c._stdin is None

    @pytest.mark.asyncio
    async def test_start_wires_process_fields(self) -> None:
        pytest.importorskip("asyncssh")
        import asyncssh

        c = _make_ssh_connector()
        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdout = MagicMock()
        mock_conn = MagicMock()
        # Peer-IP egress guard reads conn.get_extra_info("peername") post-connect;
        # a benign public peer must let start() proceed to create_process().
        mock_conn.get_extra_info = MagicMock(return_value=("93.184.216.34", 22))
        mock_conn.create_process = AsyncMock(return_value=mock_process)
        with patch.object(asyncssh, "connect", new=AsyncMock(return_value=mock_conn)):
            await c.start()
        assert c._connected is True
        assert c._stdin is mock_process.stdin
        assert c._stdout is mock_process.stdout

    def test_known_hosts_path_stored(self) -> None:
        pytest.importorskip("asyncssh")
        from provide.uterm.server.connectors.ssh import SshSessionConnector

        c = SshSessionConnector("s", "S", {"host": "h", "known_hosts": "/etc/ssh/known_hosts"})
        assert c._known_hosts == "/etc/ssh/known_hosts"

    def test_is_connected_false_when_stdin_none(self) -> None:
        c = _make_ssh_connector()
        c._connected = True
        c._stdout = AsyncMock()
        c._conn = MagicMock()
        c._stdin = None
        assert not c.is_connected()

    @pytest.mark.asyncio
    async def test_stop_with_stdin_none_skips_write_eof(self) -> None:
        """Line 164->167: stdin is None at stop time → write_eof is skipped."""
        c = _make_ssh_connector()
        mock_conn = MagicMock()
        mock_conn.close = MagicMock()
        mock_conn.wait_closed = AsyncMock()
        mock_process = MagicMock()
        mock_process.close = MagicMock()
        c._conn = mock_conn
        c._process = mock_process
        c._stdin = None  # stdin is None — should skip write_eof
        c._stdout = AsyncMock()
        c._connected = True
        await c.stop()
        mock_process.close.assert_called_once()
        mock_conn.close.assert_called_once()
        assert c._stdin is None

    @pytest.mark.asyncio
    async def test_stop_with_conn_none_skips_close(self) -> None:
        """Line 170->exit: conn is None at stop time → conn.close() is skipped."""
        c = _make_ssh_connector()
        mock_stdin = MagicMock()
        mock_stdin.write_eof = MagicMock()
        mock_process = MagicMock()
        mock_process.close = MagicMock()
        c._conn = None  # conn is None — should skip close
        c._process = mock_process
        c._stdin = mock_stdin
        c._stdout = AsyncMock()
        c._connected = True
        await c.stop()
        mock_stdin.write_eof.assert_called_once()
        mock_process.close.assert_called_once()
        assert c._conn is None
