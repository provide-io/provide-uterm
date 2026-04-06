#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for TelnetClient and start_telnet_server."""

from __future__ import annotations

import asyncio

import pytest

from provide.terminal.transports.telnet import (
    DO,
    ECHO,
    IAC,
    NAWS,
    WILL,
    TelnetClient,
    TelnetTransport,
    start_telnet_server,
)
from tests.helpers.mock_server import MockTelnetServer


class TestTelnetConstants:
    def test_iac_value(self) -> None:
        assert IAC == 255

    def test_will_value(self) -> None:
        assert WILL == 251

    def test_do_value(self) -> None:
        assert DO == 253


class TestTelnetClient:
    async def test_connect_and_receive(self) -> None:
        async with MockTelnetServer(send=b"hello") as srv, TelnetClient("127.0.0.1", srv.port) as client:
            data = await client.read(5)
        assert data == b"hello"

    async def test_write_and_server_receives(self) -> None:
        async with MockTelnetServer() as srv:
            async with TelnetClient("127.0.0.1", srv.port) as client:
                client.write(b"ping")
                await client.drain()
            await srv.wait_for_connection()
        assert srv.received == b"ping"

    async def test_not_connected_raises(self) -> None:
        client = TelnetClient("127.0.0.1", 9999)
        with pytest.raises(RuntimeError, match="not connected"):
            await client.read(1)

    def test_will_builds_correct_bytes(self) -> None:
        client = TelnetClient("127.0.0.1", 9999)
        assert client.will(ECHO) == bytes([IAC, WILL, ECHO])

    def test_do_builds_correct_bytes(self) -> None:
        client = TelnetClient("127.0.0.1", 9999)
        assert client.do(NAWS) == bytes([IAC, DO, NAWS])


class TestStartTelnetServer:
    async def test_server_sends_handshake(self, free_port: int) -> None:
        received: list[bytes] = []
        connected = asyncio.Event()

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.close()
            connected.set()

        server = await start_telnet_server(handler, host="127.0.0.1", port=free_port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
            # Read the handshake — at least 15 bytes (5 IAC sequences × 3 bytes each)
            data = await asyncio.wait_for(reader.read(64), timeout=2.0)
            received.append(data)
            writer.close()
        finally:
            server.close()
            await server.wait_closed()

        handshake = received[0]
        # Verify handshake starts with IAC WILL ECHO
        assert handshake[:3] == bytes([IAC, WILL, ECHO])
        # Verify IAC DO NAWS is somewhere in the handshake
        assert bytes([IAC, DO, NAWS]) in handshake

    async def test_handler_called_with_reader_writer(self, free_port: int) -> None:
        handler_called = asyncio.Event()

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(b"ok")
            await writer.drain()
            writer.close()
            handler_called.set()

        server = await start_telnet_server(handler, host="127.0.0.1", port=free_port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
            # Read handshake + "ok"
            await asyncio.wait_for(reader.read(64), timeout=2.0)
            await asyncio.wait_for(handler_called.wait(), timeout=2.0)
            writer.close()
        finally:
            server.close()
            await server.wait_closed()

        assert handler_called.is_set()

    async def test_writer_closed_after_handler_returns(self, free_port: int) -> None:
        """Regression: writer must be closed by the server after handler returns normally.

        Previously the finally block around the handler was missing, causing the TCP
        connection to leak if the handler returned without closing the writer itself.
        """
        handler_done = asyncio.Event()

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            # Return immediately without closing the writer — server must close it.
            handler_done.set()

        server = await start_telnet_server(handler, host="127.0.0.1", port=free_port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
            # Drain the telnet handshake so the buffer is empty before we check EOF.
            await asyncio.wait_for(reader.read(64), timeout=2.0)
            # Wait for handler to finish, then yield so the server's finally runs.
            await asyncio.wait_for(handler_done.wait(), timeout=2.0)
            await asyncio.sleep(0.05)
            # The server must have closed its writer — client reader returns b"" (EOF).
            data = await asyncio.wait_for(reader.read(1), timeout=1.0)
            writer.close()
        finally:
            server.close()
            await server.wait_closed()

        assert data == b"", "server writer was not closed after handler returned"


# ---------------------------------------------------------------------------
# Regression: TelnetTransport cancels in-flight negotiation tasks on disconnect
# ---------------------------------------------------------------------------


class TestTelnetTransportTasksCleanup:
    async def test_disconnect_cancels_pending_tasks(self, free_port: int) -> None:
        """Regression: disconnect() must cancel and clear any in-flight negotiation tasks.

        Previously self._tasks was not touched in disconnect(), leaving orphaned
        asyncio tasks running against a closed writer on repeated connect/disconnect
        cycles.
        """
        transport = TelnetTransport()

        # Connect to a simple echo-type server that accepts and closes.
        handler_ready = asyncio.Event()

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            handler_ready.set()
            # Keep writer open long enough for connect to succeed, then close.
            await asyncio.sleep(0.2)
            writer.close()

        server = await start_telnet_server(handler, host="127.0.0.1", port=free_port)
        try:
            await transport.connect("127.0.0.1", free_port)
            await asyncio.wait_for(handler_ready.wait(), timeout=2.0)
            # Inject a fake task into _tasks to simulate in-flight negotiation.
            fake_task = asyncio.create_task(asyncio.sleep(60))
            transport._tasks.add(fake_task)
            assert not fake_task.cancelled()

            await transport.disconnect()

            # Yield to let the cancellation propagate.
            await asyncio.sleep(0)

            # After disconnect, _tasks must be empty and the fake task cancelled.
            assert len(transport._tasks) == 0
            assert fake_task.cancelled()
        finally:
            server.close()
            await server.wait_closed()

    async def test_disconnect_noop_when_tasks_empty(self, free_port: int) -> None:
        """disconnect() with no pending tasks must not raise."""
        transport = TelnetTransport()

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.close()

        server = await start_telnet_server(handler, host="127.0.0.1", port=free_port)
        try:
            await transport.connect("127.0.0.1", free_port)
            assert len(transport._tasks) == 0
            await transport.disconnect()  # must not raise
        finally:
            server.close()
            await server.wait_closed()


class TestTelnetClientCloseWhenNotConnected:
    async def test_close_when_writer_none_is_noop(self) -> None:
        """Line 91->exit: close() when _writer is None should be a no-op."""
        client = TelnetClient("127.0.0.1", 9999)
        # _writer is None by default (not connected)
        assert client._writer is None
        await client.close()  # should not raise
        assert client._writer is None
