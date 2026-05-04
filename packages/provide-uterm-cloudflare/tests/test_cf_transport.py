#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for CFWebSocketStreamReader and CFWebSocketStreamWriter."""

from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import MagicMock

from provide.terminal.cloudflare.cf_transport import (
    CFWebSocketStreamReader,
    CFWebSocketStreamWriter,
)

# ---------------------------------------------------------------------------
# CFWebSocketStreamReader
# ---------------------------------------------------------------------------


class TestCFWebSocketStreamReader:
    """Tests for the event-based WebSocket-to-StreamReader adapter."""

    async def test_read_from_queued_message(self) -> None:
        reader = CFWebSocketStreamReader()
        reader.queue_message("hello")
        result = await reader.read(5)
        assert result == b"hello"

    async def test_read_partial(self) -> None:
        reader = CFWebSocketStreamReader()
        reader.queue_message("abcdef")
        result = await reader.read(3)
        assert result == b"abc"
        # remainder still buffered
        result2 = await reader.read(3)
        assert result2 == b"def"

    async def test_read_multiple_messages(self) -> None:
        reader = CFWebSocketStreamReader()
        reader.queue_message("ab")
        reader.queue_message("cd")
        # reads across two messages
        result = await reader.read(4)
        assert result == b"abcd"

    async def test_read_blocks_until_message(self) -> None:
        reader = CFWebSocketStreamReader()

        async def feed_later() -> None:
            await asyncio.sleep(0.05)
            reader.queue_message("x")

        asyncio.create_task(feed_later())  # noqa: RUF006
        result = await asyncio.wait_for(reader.read(1), timeout=2.0)
        assert result == b"x"

    async def test_read_returns_empty_when_closed(self) -> None:
        reader = CFWebSocketStreamReader()
        reader.close()
        result = await reader.read(10)
        assert result == b""

    async def test_close_unblocks_waiting_read(self) -> None:
        reader = CFWebSocketStreamReader()

        async def close_later() -> None:
            await asyncio.sleep(0.05)
            reader.close()

        asyncio.create_task(close_later())  # noqa: RUF006
        result = await asyncio.wait_for(reader.read(1), timeout=2.0)
        assert result == b""

    async def test_queue_message_ignored_when_closed(self) -> None:
        reader = CFWebSocketStreamReader()
        reader.close()
        reader.queue_message("ignored")
        result = await reader.read(1)
        assert result == b""

    async def test_queue_empty_string_ignored(self) -> None:
        reader = CFWebSocketStreamReader()

        async def close_later() -> None:
            await asyncio.sleep(0.05)
            reader.close()

        reader.queue_message("")  # should be ignored
        asyncio.create_task(close_later())  # noqa: RUF006
        result = await asyncio.wait_for(reader.read(1), timeout=2.0)
        assert result == b""

    async def test_custom_encoding(self) -> None:
        reader = CFWebSocketStreamReader(encoding="utf-8")
        reader.queue_message("\u00e9")  # é
        result = await reader.read(2)
        assert result == b"\xc3\xa9"

    async def test_close_while_waiting_sets_result(self) -> None:
        """Close unblocks a pending read via _waiting future."""
        reader = CFWebSocketStreamReader()

        async def close_soon() -> None:
            await asyncio.sleep(0.02)
            reader.close()

        asyncio.create_task(close_soon())  # noqa: RUF006
        result = await asyncio.wait_for(reader.read(5), timeout=2.0)
        assert result == b""

    async def test_close_during_empty_queue_returns_empty(self) -> None:
        """When buffer is partially filled but queue empty and then closed."""
        reader = CFWebSocketStreamReader()
        reader.queue_message("a")  # only 1 byte available

        async def close_soon() -> None:
            await asyncio.sleep(0.02)
            reader.close()

        asyncio.create_task(close_soon())  # noqa: RUF006
        # requesting 5 bytes, only 1 available, then close
        result = await asyncio.wait_for(reader.read(5), timeout=2.0)
        assert result == b""

    async def test_closed_with_empty_queue_no_waiting(self) -> None:
        """Close during queue consumption → hits line 70-71 (closed + empty queue).

        We replace the message_queue with a custom deque subclass whose
        popleft() also marks the reader closed, simulating an external
        close that happens between consuming the last message and the
        next loop iteration's empty-queue check.
        """

        class ClosingDeque(deque):  # type: ignore[type-arg]
            def __init__(self, reader_ref: CFWebSocketStreamReader) -> None:
                super().__init__()
                self._reader_ref = reader_ref

            def popleft(self) -> str:
                val: str = super().popleft()
                self._reader_ref._closed = True
                return val

        reader = CFWebSocketStreamReader()
        cq: deque[str] = ClosingDeque(reader)
        reader._message_queue = cq
        reader.queue_message("a")  # 1 byte — won't satisfy read(5)
        result = await reader.read(5)
        assert result == b""


# ---------------------------------------------------------------------------
# CFWebSocketStreamWriter
# ---------------------------------------------------------------------------


class TestCFWebSocketStreamWriter:
    """Tests for the WebSocket StreamWriter adapter."""

    async def test_write_and_drain(self) -> None:
        ws = MagicMock()
        writer = CFWebSocketStreamWriter(ws)
        writer.write(b"hello")
        await writer.drain()
        ws.send.assert_called_once_with("hello")

    async def test_write_accumulates(self) -> None:
        ws = MagicMock()
        writer = CFWebSocketStreamWriter(ws)
        writer.write(b"he")
        writer.write(b"llo")
        await writer.drain()
        ws.send.assert_called_once_with("hello")

    async def test_drain_noop_when_empty(self) -> None:
        ws = MagicMock()
        writer = CFWebSocketStreamWriter(ws)
        await writer.drain()
        ws.send.assert_not_called()

    async def test_write_ignored_when_closed(self) -> None:
        ws = MagicMock()
        writer = CFWebSocketStreamWriter(ws)
        writer.close()
        writer.write(b"ignored")
        await writer.drain()
        ws.send.assert_not_called()

    async def test_drain_noop_when_closed(self) -> None:
        ws = MagicMock()
        writer = CFWebSocketStreamWriter(ws)
        writer.write(b"data")
        writer.close()
        await writer.drain()
        ws.send.assert_not_called()

    async def test_get_extra_info_peername(self) -> None:
        ws = MagicMock()
        writer = CFWebSocketStreamWriter(ws, peername=("1.2.3.4", 8080))
        assert writer.get_extra_info("peername") == ("1.2.3.4", 8080)

    async def test_get_extra_info_default(self) -> None:
        ws = MagicMock()
        writer = CFWebSocketStreamWriter(ws)
        assert writer.get_extra_info("peername") == ("unknown", 0)
        assert writer.get_extra_info("something_else") is None
        assert writer.get_extra_info("something_else", 42) == 42

    async def test_wait_closed_noop(self) -> None:
        ws = MagicMock()
        writer = CFWebSocketStreamWriter(ws)
        await writer.wait_closed()  # should not raise

    async def test_begin_batch_suppresses_drain(self) -> None:
        ws = MagicMock()
        writer = CFWebSocketStreamWriter(ws)
        writer.begin_batch()
        writer.write(b"data1")
        await writer.drain()  # should be suppressed
        ws.send.assert_not_called()

    async def test_flush_batch_sends_all(self) -> None:
        ws = MagicMock()
        writer = CFWebSocketStreamWriter(ws)
        writer.begin_batch()
        writer.write(b"part1")
        writer.write(b"part2")
        await writer.drain()  # suppressed
        await writer.flush_batch()
        ws.send.assert_called_once_with("part1part2")

    async def test_drain_exception_closes_writer(self) -> None:
        ws = MagicMock()
        ws.send.side_effect = RuntimeError("ws closed")
        writer = CFWebSocketStreamWriter(ws)
        writer.write(b"data")
        await writer.drain()  # should not raise, but marks closed
        writer.write(b"more")
        await writer.drain()
        # second send never called because writer is closed
        assert ws.send.call_count == 1
