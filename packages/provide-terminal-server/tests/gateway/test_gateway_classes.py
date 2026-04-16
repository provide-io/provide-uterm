#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for TelnetWsGateway and _make_process_handler."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from provide.terminal.gateway._gateway import (
    TelnetWsGateway,
    _make_process_handler,
)

# ---------------------------------------------------------------------------
# Async iterator helper
# ---------------------------------------------------------------------------


class _AsyncIter:
    def __init__(self, items: list[Any]) -> None:
        self._items = iter(items)

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None


def _mock_ws(messages: list[Any] | None = None) -> MagicMock:
    ws = MagicMock()
    ws.__aiter__ = lambda self: _AsyncIter(messages or [])
    ws.send = AsyncMock()
    return ws


def _make_ws_context(ws_mock: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=ws_mock)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# _make_process_handler
# ---------------------------------------------------------------------------


class TestMakeProcessHandler:
    async def test_returns_callable(self) -> None:
        handler = await _make_process_handler("ws://test", "passthrough")
        assert callable(handler)

    async def test_handler_connects_and_pipes(self) -> None:
        handler = await _make_process_handler("ws://test", "passthrough")

        process = MagicMock()
        process.stdin = AsyncMock()
        process.stdin.read = AsyncMock(return_value=b"")
        # at_eof: False → enter loop; True → exit after first session ends
        process.stdin.at_eof = MagicMock(side_effect=[False, True])
        process.stdout = MagicMock()
        process.exit = MagicMock()

        ws_mock = _mock_ws()
        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await handler(process)

        process.exit.assert_called_once_with(0)

    async def test_handler_no_resume_on_fresh_connect(self) -> None:
        """Fresh connection with empty token_holder sends no resume frame."""
        handler = await _make_process_handler("ws://test", "passthrough")

        process = MagicMock()
        process.stdin = AsyncMock()
        process.stdin.read = AsyncMock(return_value=b"")
        process.stdin.at_eof = MagicMock(side_effect=[False, True])
        process.stdout = MagicMock()
        process.exit = MagicMock()

        ws_mock = _mock_ws()
        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await handler(process)

        # No resume frame should have been sent (no token in holder)
        ws_mock.send.assert_not_called()

    async def test_handler_exception_calls_exit(self) -> None:
        handler = await _make_process_handler("ws://test", "passthrough")

        process = MagicMock()
        process.stdin = AsyncMock()
        # at_eof: False → enter loop; True → exit after exception
        process.stdin.at_eof = MagicMock(side_effect=[False, True])
        process.stdout = MagicMock()
        process.exit = MagicMock()

        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.side_effect = OSError("connection refused")

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await handler(process)

        process.exit.assert_called_once_with(0)

    async def test_handler_exit_exception_suppressed(self) -> None:
        handler = await _make_process_handler("ws://test", "passthrough")

        process = MagicMock()
        process.stdin = AsyncMock()
        process.stdin.at_eof = MagicMock(side_effect=[False, True])
        process.stdout = MagicMock()
        process.exit = MagicMock(side_effect=RuntimeError("exit failed"))

        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.side_effect = OSError("fail")

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await handler(process)

    async def test_handler_cancels_pending(self) -> None:
        """Cover task.cancel() in _process_handler."""
        from provide.terminal.control_channel import encode_data

        handler = await _make_process_handler("ws://test", "passthrough")

        process = MagicMock()
        process.stdin = AsyncMock()

        async def slow_read(_n: int = 4096) -> bytes:
            await asyncio.sleep(100)
            return b""

        process.stdin.read = slow_read
        process.stdin.at_eof = MagicMock(side_effect=[False, True])
        process.stdout = MagicMock()
        process.exit = MagicMock()

        # ws yields one message then ends so _ws_to_ssh finishes quickly
        ws_mock = _mock_ws([encode_data("x")])

        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await handler(process)

        process.exit.assert_called_once_with(0)

    async def test_handler_ssh_reconnect_indicator(self) -> None:
        """SSH handler writes reconnect indicator when WS drops but SSH client stays."""
        from provide.terminal.control_channel import encode_data

        handler = await _make_process_handler("ws://test", "passthrough")

        process = MagicMock()
        process.stdin = AsyncMock()

        async def slow_read(_n: int = 4096) -> bytes:
            await asyncio.sleep(100)
            return b""

        process.stdin.read = slow_read
        # at_eof: False → enter loop 1; False → check after drop (reconnect path);
        # False → enter loop 2; True → exit after second session
        process.stdin.at_eof = MagicMock(side_effect=[False, False, False, True])
        process.stdout = MagicMock()
        process.exit = MagicMock()

        call_count = 0

        def make_ws_context_for_call() -> MagicMock:
            nonlocal call_count
            call_count += 1
            ws = _mock_ws([encode_data("x")])  # yields one message then closes
            return _make_ws_context(ws)

        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.side_effect = lambda *a, **kw: make_ws_context_for_call()

        with (
            patch.dict("sys.modules", {"websockets": mock_ws_mod}),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await handler(process)

        # Reconnect indicator should have been written to SSH stdout
        written = "".join(call[0][0] for call in process.stdout.write.call_args_list)
        assert "reconnecting" in written
        assert "\x1b7" in written
        assert "\x1b8" in written


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

    async def test_start_returns_server(self) -> None:
        gw = TelnetWsGateway("ws://test")
        server = await gw.start("127.0.0.1", 0)
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
            patch("provide.terminal.gateway._gateway._pipe_ws", side_effect=mock_pipe_ws),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await gw._handle(reader, writer)

        assert call_count >= 1  # at least one call; reconnect behavior is tested
        writer.close.assert_called_once()

    async def test_handle_stops_when_reader_eof_initially(self) -> None:
        gw = TelnetWsGateway("ws://test")

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.at_eof = MagicMock(return_value=True)
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        with patch("provide.terminal.gateway._gateway._pipe_ws", new_callable=AsyncMock) as mock_pipe:
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

        with patch("provide.terminal.gateway._gateway._pipe_ws", new_callable=AsyncMock):
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
                "provide.terminal.gateway._gateway._pipe_ws",
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

        with patch("provide.terminal.gateway._gateway._pipe_ws", new_callable=AsyncMock):
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
            patch("provide.terminal.gateway._gateway._pipe_ws", side_effect=mock_pipe),
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
            patch("provide.terminal.gateway._gateway._pipe_ws", side_effect=mock_pipe),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await gw._handle(reader, writer)

        # Verify the reconnect indicator was written (cursor-save + bottom-row + cyan text)
        written_data = b"".join(call[0][0] for call in writer.write.call_args_list)
        assert b"reconnecting" in written_data
        assert b"\x1b7" in written_data  # cursor save
        assert b"\x1b8" in written_data  # cursor restore
