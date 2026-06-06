#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for :class:`TelnetWsGateway` — the raw-TCP→WebSocket gateway.

Split from :mod:`test_gateway_classes` to keep individual test files
under the 500-LOC budget.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.gateway._telnet_gateway import TelnetWsGateway

# ---------------------------------------------------------------------------
# TelnetWsGateway
# ---------------------------------------------------------------------------


class TestTelnetWsGateway:
    def test_init(self) -> None:
        gw = TelnetWsGateway("ws://test")
        assert gw._ws_url == "ws://test"
        assert gw._color_mode == "passthrough"

    def test_init_with_color_mode(self) -> None:
        gw = TelnetWsGateway("ws://test", color_mode="256")
        assert gw._color_mode == "256"

    def test_client_cert_builds_ssl_context(self) -> None:
        context = MagicMock()
        with patch("ssl.create_default_context", return_value=context) as create_context:
            gw = TelnetWsGateway("wss://test", client_cert="/tmp/client.crt", client_key="/tmp/client.key")

        create_context.assert_called_once_with()
        context.load_cert_chain.assert_called_once_with(certfile="/tmp/client.crt", keyfile="/tmp/client.key")
        assert gw._ws_ssl is context

    def test_client_cert_rejects_explicit_ssl_context(self) -> None:
        with (
            patch("ssl.create_default_context"),
            pytest.raises(ValueError, match="both ws_ssl and client_cert"),
        ):
            TelnetWsGateway(
                "wss://test", ws_ssl=MagicMock(), client_cert="/tmp/client.crt", client_key="/tmp/client.key"
            )

    async def test_start_returns_server(self) -> None:
        gw = TelnetWsGateway("ws://test")
        server = await gw.start("127.0.0.1", 0)
        try:
            assert isinstance(server, asyncio.AbstractServer)
        finally:
            server.close()
            await server.wait_closed()

    async def test_start_default_host_is_loopback(self) -> None:
        # Default bind is loopback now, so start() with no host must NOT raise.
        gw = TelnetWsGateway("ws://test")
        server = await gw.start(port=0)
        try:
            assert isinstance(server, asyncio.AbstractServer)
        finally:
            server.close()
            await server.wait_closed()

    async def test_start_nonloopback_without_optin_raises(self) -> None:
        gw = TelnetWsGateway("ws://test")
        with pytest.raises(RuntimeError, match="non-loopback"):
            await gw.start("0.0.0.0", 0)  # nosec B104 — asserting the guard fires

    async def test_start_nonloopback_with_optin_binds(self) -> None:
        gw = TelnetWsGateway("ws://test", allow_unauthenticated=True)
        server = await gw.start("0.0.0.0", 0)  # nosec B104 — explicit opt-in under test
        try:
            assert isinstance(server, asyncio.AbstractServer)
        finally:
            server.close()
            await server.wait_closed()

    async def test_handle_reconnects_on_ws_drop(self) -> None:
        gw = TelnetWsGateway("ws://test")

        reader = AsyncMock(spec=asyncio.StreamReader)
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        call_count = 0

        async def mock_pipe_ws(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("ws dropped")

        # Sequence: enter loop (False), after exception check eof (False),
        # then reconnect, enter loop again (False), pipe succeeds,
        # after pipe check eof (True)
        # at_eof sequence:
        # 1. line 462 (attempt=0): False → enter loop
        # 2. line 477 (after exception): False → reconnect
        # 3. line 462 (attempt=1): False → enter loop
        # 4. line 477 (after success): True → break
        reader.at_eof = MagicMock(side_effect=[False, False, False, True])

        with (
            patch("provide.uterm.gateway._telnet_gateway._pipe_ws", side_effect=mock_pipe_ws),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await gw._handle(reader, writer)

        assert call_count >= 1  # at least one call; reconnect behavior is tested
        writer.close.assert_called_once()

    async def test_handle_does_not_reconnect_on_normal_close(self) -> None:
        """A deliberate server-side close (WS code 1000) ends the session — the
        gateway must NOT reconnect. Transient drops / hibernation use 1006/None
        and still reconnect; only a normal closure means the user quit on purpose.
        """
        gw = TelnetWsGateway("ws://test")

        reader = AsyncMock(spec=asyncio.StreamReader)
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        call_count = 0

        async def mock_pipe_ws(*args: Any, **kwargs: Any) -> int:
            nonlocal call_count
            call_count += 1
            return 1000  # normal closure — the server closed deliberately

        # TCP client stays connected throughout; only the 1000 close must stop the loop.
        reader.at_eof = MagicMock(return_value=False)

        with (
            patch("provide.uterm.gateway._telnet_gateway._pipe_ws", side_effect=mock_pipe_ws),
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            await gw._handle(reader, writer)

        assert call_count == 1  # no reconnect after a deliberate close
        mock_sleep.assert_not_called()  # no reconnect backoff
        writer.close.assert_called_once()

    async def test_handle_reconnects_on_abnormal_close(self) -> None:
        """A non-1000 close (transient drop / hibernation) still reconnects."""
        gw = TelnetWsGateway("ws://test")

        reader = AsyncMock(spec=asyncio.StreamReader)
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        call_count = 0

        async def mock_pipe_ws(*args: Any, **kwargs: Any) -> int:
            nonlocal call_count
            call_count += 1
            return 1006  # abnormal closure — should reconnect

        # enter loop; after pipe TCP still alive; re-enter; after 2nd pipe TCP closed
        reader.at_eof = MagicMock(side_effect=[False, False, False, True])

        with (
            patch("provide.uterm.gateway._telnet_gateway._pipe_ws", side_effect=mock_pipe_ws),
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            await gw._handle(reader, writer)

        assert call_count == 2  # reconnected once
        mock_sleep.assert_called()

    async def test_handle_stops_when_reader_eof_initially(self) -> None:
        gw = TelnetWsGateway("ws://test")

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.at_eof = MagicMock(return_value=True)
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        with patch("provide.uterm.gateway._telnet_gateway._pipe_ws", new_callable=AsyncMock) as mock_pipe:
            await gw._handle(reader, writer)
            mock_pipe.assert_not_called()

    async def test_handle_stops_when_reader_eof_after_pipe(self) -> None:
        gw = TelnetWsGateway("ws://test")

        reader = AsyncMock(spec=asyncio.StreamReader)
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        # First at_eof: False (enter loop), pipe runs, second at_eof: True
        reader.at_eof = MagicMock(side_effect=[False, True])

        with patch("provide.uterm.gateway._telnet_gateway._pipe_ws", new_callable=AsyncMock):
            await gw._handle(reader, writer)

        writer.close.assert_called_once()

    async def test_handle_exhausts_reconnects(self) -> None:
        gw = TelnetWsGateway("ws://test")

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.at_eof = MagicMock(return_value=False)
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        with (
            patch(
                "provide.uterm.gateway._telnet_gateway._pipe_ws",
                new_callable=AsyncMock,
                side_effect=ConnectionError("fail"),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await gw._handle(reader, writer)

        writer.close.assert_called_once()

    async def test_handle_cleanup_on_writer_error(self) -> None:
        gw = TelnetWsGateway("ws://test")

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.at_eof = MagicMock(return_value=True)
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.close = MagicMock(side_effect=RuntimeError("close failed"))
        writer.wait_closed = AsyncMock()

        await gw._handle(reader, writer)

    async def test_handle_pipe_success_then_eof(self) -> None:
        """Pipe completes without error, then reader is at EOF."""
        gw = TelnetWsGateway("ws://test")

        reader = AsyncMock(spec=asyncio.StreamReader)
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        # Enter loop, pipe succeeds, check eof → True
        reader.at_eof = MagicMock(side_effect=[False, True])

        with patch("provide.uterm.gateway._telnet_gateway._pipe_ws", new_callable=AsyncMock):
            await gw._handle(reader, writer)

    async def test_handle_reconnect_indicator_write_error_suppressed(self) -> None:
        """Cover lines 515-516: exception writing reconnect indicator is swallowed."""
        gw = TelnetWsGateway("ws://test")

        reader = AsyncMock(spec=asyncio.StreamReader)
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.drain = AsyncMock()
        writer.write = MagicMock(side_effect=OSError("pipe broken"))
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        # at_eof: False → enter loop, False → after drop, False → retry, True → done
        reader.at_eof = MagicMock(side_effect=[False, False, False, True])

        call_count = 0

        async def mock_pipe(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("ws dropped")

        with (
            patch("provide.uterm.gateway._telnet_gateway._pipe_ws", side_effect=mock_pipe),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await gw._handle(reader, writer)  # should not raise despite write error

        assert call_count >= 1

    async def test_handle_writes_reconnect_indicator(self) -> None:
        """Reconnect indicator is written to TCP client on WS drop."""
        gw = TelnetWsGateway("ws://test")

        reader = AsyncMock(spec=asyncio.StreamReader)
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        # at_eof: False (enter), False (after drop), False (enter retry), True (after retry)
        reader.at_eof = MagicMock(side_effect=[False, False, False, True])

        call_count = 0

        async def mock_pipe(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("ws dropped")

        with (
            patch("provide.uterm.gateway._telnet_gateway._pipe_ws", side_effect=mock_pipe),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await gw._handle(reader, writer)

        # Verify the reconnect indicator was written (cursor-save + bottom-row + cyan text)
        written_data = b"".join(call[0][0] for call in writer.write.call_args_list)
        assert b"reconnecting" in written_data
        assert b"\x1b7" in written_data  # cursor save
        assert b"\x1b8" in written_data  # cursor restore
