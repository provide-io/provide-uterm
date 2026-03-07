#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.gateway — TelnetWsGateway (and helpers)."""

from __future__ import annotations

import asyncio

import pytest
import websockets
import websockets.server

from provide.terminal.gateway import TelnetWsGateway, _pipe_ws, _tcp_to_ws, _ws_to_tcp

# ---------------------------------------------------------------------------
# Pump helper unit tests
# ---------------------------------------------------------------------------


class TestTcpToWs:
    async def test_forwards_bytes_as_text(self) -> None:
        sent: list[str] = []

        class MockWs:
            async def send(self, data: str) -> None:
                sent.append(data)

        reader = asyncio.StreamReader()
        reader.feed_data(b"hello")
        reader.feed_eof()

        await _tcp_to_ws(reader, MockWs())
        assert sent == ["hello"]

    async def test_stops_on_eof(self) -> None:
        sent: list[str] = []

        class MockWs:
            async def send(self, data: str) -> None:
                sent.append(data)

        reader = asyncio.StreamReader()
        reader.feed_eof()

        await _tcp_to_ws(reader, MockWs())
        assert sent == []


def _async_iter(items):
    """Return an async iterator over *items*."""

    async def _gen():
        for item in items:
            yield item

    return _gen()


class TestWsToTcp:
    async def test_forwards_str_message(self) -> None:
        written: list[bytes] = []
        drained: list[bool] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                drained.append(True)

        from asyncio import StreamWriter
        from typing import cast
        await _ws_to_tcp(_async_iter(["world"]), cast("StreamWriter", MockWriter()))
        assert written == [b"world"]
        assert drained

    async def test_forwards_bytes_message(self) -> None:
        written: list[bytes] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                pass

        from asyncio import StreamWriter
        from typing import cast
        await _ws_to_tcp(_async_iter([b"\xff\xfe"]), cast("StreamWriter", MockWriter()))
        assert written == [b"\xff\xfe"]


# ---------------------------------------------------------------------------
# TelnetWsGateway — integration via real local WS server
# ---------------------------------------------------------------------------


from typing import Any


async def _start_ws_echo_server(banner: bytes = b"") -> tuple[Any, int]:
    """Start a local WebSocket server that optionally sends a banner then echoes."""

    async def handler(ws: websockets.ServerConnection) -> None:
        if banner:
            await ws.send(banner.decode("latin-1"))
        async for msg in ws:
            await ws.send(msg)

    srv = await websockets.serve(handler, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    return srv, port


class TestTelnetWsGateway:
    async def test_start_returns_server(self) -> None:
        ws_srv, ws_port = await _start_ws_echo_server()
        try:
            gw = TelnetWsGateway(f"ws://127.0.0.1:{ws_port}")
            tcp_srv = await gw.start("127.0.0.1", 0)
            assert tcp_srv is not None
            tcp_srv.close()
        finally:
            ws_srv.close()

    async def test_banner_forwarded_to_telnet_client(self) -> None:
        """Data sent by the WS server arrives at the telnet client."""
        banner = b"Welcome to warp!\r\n"
        ws_srv, ws_port = await _start_ws_echo_server(banner=banner)
        try:
            gw = TelnetWsGateway(f"ws://127.0.0.1:{ws_port}")
            tcp_srv = await gw.start("127.0.0.1", 0)
            from asyncio import Server
            assert isinstance(tcp_srv, Server)
            assert tcp_srv.sockets is not None
            tcp_port = tcp_srv.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
            data = await asyncio.wait_for(reader.read(256), timeout=2.0)
            writer.close()
            tcp_srv.close()
        finally:
            ws_srv.close()

        assert banner.decode("utf-8") in data.decode("utf-8")

    async def test_client_data_forwarded_to_ws(self) -> None:
        """Data sent by the telnet client is echoed back via the WS server."""
        ws_srv, ws_port = await _start_ws_echo_server()
        try:
            gw = TelnetWsGateway(f"ws://127.0.0.1:{ws_port}")
            tcp_srv = await gw.start("127.0.0.1", 0)
            from asyncio import Server
            assert isinstance(tcp_srv, Server)
            assert tcp_srv.sockets is not None
            tcp_port = tcp_srv.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
            writer.write(b"ping")
            await writer.drain()
            data = await asyncio.wait_for(reader.read(256), timeout=2.0)
            writer.close()
            tcp_srv.close()
        finally:
            ws_srv.close()

        assert b"ping" in data

    async def test_disconnect_cleans_up(self) -> None:
        """Closing the telnet client closes cleanly without hanging."""
        ws_srv, ws_port = await _start_ws_echo_server()
        try:
            gw = TelnetWsGateway(f"ws://127.0.0.1:{ws_port}")
            tcp_srv = await gw.start("127.0.0.1", 0)
            from asyncio import Server
            assert isinstance(tcp_srv, Server)
            assert tcp_srv.sockets is not None
            tcp_port = tcp_srv.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
            writer.close()
            # Brief pause to let gateway detect disconnect
            await asyncio.sleep(0.1)
            tcp_srv.close()
        finally:
            ws_srv.close()

    async def test_missing_websockets_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        original = sys.modules.get("websockets")
        sys.modules["websockets"] = None  # type: ignore[assignment]
        try:
            with pytest.raises(ImportError, match="websockets"):
                TelnetWsGateway("ws://localhost:9999")
        finally:
            if original is None:
                sys.modules.pop("websockets", None)
            else:
                sys.modules["websockets"] = original


# ---------------------------------------------------------------------------
# _pipe_ws integration
# ---------------------------------------------------------------------------


class TestPipeWs:
    async def test_pipe_ws_connects_and_exits_on_eof(self) -> None:
        """_pipe_ws connects to a WS server and exits cleanly when reader hits EOF."""
        ws_srv, ws_port = await _start_ws_echo_server()
        try:
            reader = asyncio.StreamReader()
            reader.feed_eof()  # immediate EOF → _tcp_to_ws exits quickly

            class MockWriter:
                def write(self, data: bytes) -> None:
                    pass

                async def drain(self) -> None:
                    pass

                def close(self) -> None:
                    pass

                async def wait_closed(self) -> None:
                    pass

            # Should complete without hanging
            from asyncio import StreamWriter
            from typing import cast
            await asyncio.wait_for(
                _pipe_ws(reader, cast("StreamWriter", MockWriter()), f"ws://127.0.0.1:{ws_port}"),
                timeout=3.0,
            )
        finally:
            ws_srv.close()


class TestSshWsGatewayInit:
    def test_init_requires_asyncssh(self) -> None:
        """SshWsGateway can be created when asyncssh is available."""
        import asyncssh  # noqa: F401

        from provide.terminal.gateway import SshWsGateway

        gw = SshWsGateway("wss://example.com/ws")
        assert gw._ws_url == "wss://example.com/ws"
        assert gw._server_key is None

    def test_init_with_server_key(self, tmp_path) -> None:
        from provide.terminal.gateway import SshWsGateway

        key_path = tmp_path / "key.pem"
        key_path.write_text("dummy")
        gw = SshWsGateway("wss://example.com/ws", server_key=str(key_path))
        assert gw._server_key == str(key_path)


class TestSshWsGatewayStart:
    async def test_start_ephemeral_key(self) -> None:
        """SshWsGateway.start() creates an asyncssh server with ephemeral key."""
        import asyncssh

        from provide.terminal.gateway import SshWsGateway

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

        from provide.terminal.gateway import SshWsGateway

        key = asyncssh.generate_private_key("ssh-ed25519")
        key_path = tmp_path / "host_key"
        key_path.write_bytes(key.export_private_key())

        gw = SshWsGateway("wss://example.com/ws", server_key=str(key_path))
        srv = await gw.start("127.0.0.1", 0)
        import asyncssh
        assert isinstance(srv, asyncssh.SSHAcceptor)
        try:
            pass
        finally:
            srv.close()
            await srv.wait_closed()

    async def test_start_missing_key_file_raises(self, tmp_path) -> None:
        """SshWsGateway.start() raises FileNotFoundError for a missing key path."""
        from provide.terminal.gateway import SshWsGateway

        gw = SshWsGateway("wss://example.com/ws", server_key=str(tmp_path / "no_such_key"))
        with pytest.raises(FileNotFoundError, match="SSH host key not found"):
            await gw.start("127.0.0.1", 0)

    async def test_start_key_path_is_directory_raises(self, tmp_path) -> None:
        """SshWsGateway.start() raises ValueError when key path is a directory."""
        from provide.terminal.gateway import SshWsGateway

        gw = SshWsGateway("wss://example.com/ws", server_key=str(tmp_path))
        with pytest.raises(ValueError, match="not a file"):
            await gw.start("127.0.0.1", 0)


class TestSshToWs:
    async def test_ssh_to_ws_str_data(self) -> None:
        from provide.terminal.gateway import _ssh_to_ws

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
        from provide.terminal.gateway import _ssh_to_ws

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
        from provide.terminal.gateway import _ssh_to_ws

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


class TestWsToSsh:
    async def test_ws_to_ssh_str_message(self) -> None:
        from provide.terminal.gateway import _ws_to_ssh

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
        from provide.terminal.gateway import _ws_to_ssh

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


class TestTelnetWsGatewayHandleException:
    async def test_handle_exception_closes_writer(self) -> None:
        """_handle closes writer even when _pipe_ws raises."""
        from unittest.mock import AsyncMock, MagicMock, patch

        gw = TelnetWsGateway("ws://localhost")
        reader = MagicMock()
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        with patch("provide.terminal.gateway._pipe_ws", side_effect=RuntimeError("boom")):
            await gw._handle(reader, writer)

        writer.close.assert_called()
