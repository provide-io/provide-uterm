#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for CFWebSocketStreamReader and CFWebSocketStreamWriter."""

from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import MagicMock

from provide.uterm.cf_ws_transport import (
    MAX_MESSAGE_QUEUE_DEPTH,
    MAX_READER_BUFFER_BYTES,
    MAX_WRITER_PENDING_BYTES,
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

    async def test_drain_round_trips_non_utf8_bytes(self) -> None:
        """Raw terminal bytes (incl. non-UTF-8) survive write→drain losslessly.

        The matching reader decodes with latin-1 for CP437 preservation, so the
        writer must encode with latin-1 too. A utf-8/errors="replace" writer
        would map every high byte to U+FFFD before transmission, permanently
        corrupting CP437 line-drawing chars and 8-bit binary.
        """
        ws = MagicMock()
        writer = CFWebSocketStreamWriter(ws)
        # Every possible byte value, including non-UTF-8 sequences.
        original = bytes(range(256))
        writer.write(original)
        await writer.drain()
        ws.send.assert_called_once()
        sent_text = ws.send.call_args.args[0]
        # Perfect round-trip — no U+FFFD replacement characters.
        assert "�" not in sent_text
        assert sent_text.encode("latin-1") == original

    async def test_drain_round_trips_cp437_box_drawing(self) -> None:
        """CP437 box-drawing bytes round-trip exactly through drain."""
        ws = MagicMock()
        writer = CFWebSocketStreamWriter(ws)
        original = b"\xc4\xb3\xda"  # ─ │ ┌ in CP437
        writer.write(original)
        await writer.drain()
        sent_text = ws.send.call_args.args[0]
        assert "�" not in sent_text
        assert sent_text.encode("latin-1") == original


# ---------------------------------------------------------------------------
# Bounded growth
# ---------------------------------------------------------------------------


class TestBoundedGrowth:
    """Caps that stop a runaway producer from ballooning Durable Object memory.

    Each cap drops the OLDEST data, so the newest bytes — the ones a live
    client is waiting on — always survive.
    """

    def test_queue_drops_oldest_message_at_cap(self) -> None:
        """Queueing past the depth cap evicts the oldest message, not the newest."""
        reader = CFWebSocketStreamReader()
        for i in range(MAX_MESSAGE_QUEUE_DEPTH):
            reader.queue_message(f"msg{i}")
        assert len(reader._message_queue) == MAX_MESSAGE_QUEUE_DEPTH

        reader.queue_message("newest")

        assert len(reader._message_queue) == MAX_MESSAGE_QUEUE_DEPTH
        assert reader._message_queue[0] == "msg1"  # "msg0" evicted
        assert reader._message_queue[-1] == "newest"

    async def test_reader_buffer_drops_oldest_bytes_at_cap(self) -> None:
        """A message that would overflow the reader buffer evicts oldest bytes.

        The cap lives on the FILL path, so the request has to be big enough to
        keep the loop pulling: a read whose n is already satisfied by the
        existing buffer returns without ever touching the queue.
        """
        reader = CFWebSocketStreamReader()
        reader._buffer.extend(b"A" * (MAX_READER_BUFFER_BYTES - 2))
        reader.queue_message("BBBB")

        result = await reader.read(MAX_READER_BUFFER_BYTES)

        # (cap - 2) + 4 queued = cap + 2, so the 2 oldest bytes were dropped
        # and the newest 4 — the ones a live client is waiting on — survived.
        assert len(result) == MAX_READER_BUFFER_BYTES
        assert result.endswith(b"BBBB")

    def test_writer_pending_drops_oldest_bytes_at_cap(self) -> None:
        """A write that would overflow pending output evicts oldest bytes."""
        writer = CFWebSocketStreamWriter(MagicMock())
        writer.write(b"A" * MAX_WRITER_PENDING_BYTES)

        writer.write(b"BBBB")

        assert len(writer._pending) == MAX_WRITER_PENDING_BYTES
        assert bytes(writer._pending[-4:]) == b"BBBB"

    def test_writer_pending_oversized_single_write_clears_buffer(self) -> None:
        """A single write larger than the whole cap discards all prior pending."""
        writer = CFWebSocketStreamWriter(MagicMock())
        writer.write(b"old")

        writer.write(b"N" * (MAX_WRITER_PENDING_BYTES + 10))

        # Overflow exceeded the pending length, so the buffer was cleared
        # outright rather than sliced.
        assert b"old" not in bytes(writer._pending)

    def test_reader_close_releases_buffered_data(self) -> None:
        """close() drops queued messages so a closed reader pins no memory."""
        reader = CFWebSocketStreamReader()
        reader.queue_message("queued")
        reader._buffer.extend(b"buffered")

        reader.close()

        assert len(reader._message_queue) == 0
        assert len(reader._buffer) == 0


# ---------------------------------------------------------------------------
# Delivery observability
# ---------------------------------------------------------------------------


class TestDeliveryObservability:
    """A send that never happens must not look like one that did."""

    async def test_send_seq_counts_only_real_sends(self) -> None:
        """send_seq advances per ws.send() and stays put when drain is a no-op."""
        ws = MagicMock()
        writer = CFWebSocketStreamWriter(ws)

        writer.write(b"one")
        await writer.drain()
        writer.write(b"two")
        await writer.drain()
        assert writer.send_seq == 2

        # Nothing pending — drain must not count a send.
        await writer.drain()
        assert writer.send_seq == 2

    async def test_drain_on_closed_writer_sends_nothing(self, caplog) -> None:
        """Pending bytes on a closed writer are reported, not silently dropped."""
        ws = MagicMock()
        writer = CFWebSocketStreamWriter(ws)
        writer.write(b"unsent")
        writer._closed = True

        with caplog.at_level("INFO"):
            await writer.drain()

        ws.send.assert_not_called()
        assert writer.send_seq == 0
        assert "cf_writer_drain_skipped" in caplog.text
        assert "reason=closed" in caplog.text

    async def test_drain_while_batching_reports_deferral(self, caplog) -> None:
        """Batching defers the send and says so, distinguishing it from closed."""
        writer = CFWebSocketStreamWriter(MagicMock())
        writer.begin_batch()
        writer.write(b"deferred")

        with caplog.at_level("INFO"):
            await writer.drain()

        assert "reason=batching" in caplog.text

    async def test_send_failure_marks_closed_and_logs(self, caplog) -> None:
        """A failed ws.send() reports the error type and closes the writer."""
        ws = MagicMock()
        ws.send.side_effect = RuntimeError("bridge gone")
        writer = CFWebSocketStreamWriter(ws)
        writer.write(b"data")

        with caplog.at_level("ERROR"):
            await writer.drain()

        assert writer._closed is True
        assert writer.send_seq == 0
        assert "cf_writer_send_failed" in caplog.text
        assert "RuntimeError" in caplog.text


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


class TestEncoding:
    """Consumers differ in what they put on the wire; encoding is per-consumer."""

    async def test_writer_utf8_encoding_recovers_original_text(self) -> None:
        """A consumer whose session encodes UTF-8 gets its text back intact."""
        ws = MagicMock()
        writer = CFWebSocketStreamWriter(ws, encoding="utf-8")
        original = "┌─┐ WARP"

        writer.write(original.encode("utf-8"))
        await writer.drain()

        # Decoded as UTF-8, so ws.send() receives the real characters rather
        # than one latin-1 code point per UTF-8 byte.
        assert ws.send.call_args.args[0] == original

    async def test_writer_latin1_default_would_mangle_utf8_bytes(self) -> None:
        """The default is byte-transparent, which is why UTF-8 consumers must opt in."""
        ws = MagicMock()
        writer = CFWebSocketStreamWriter(ws)  # latin-1 default

        writer.write("┌".encode())  # 3 UTF-8 bytes

        await writer.drain()
        # Three separate code points, not one box-drawing character — correct
        # for a byte-transparent consumer, wrong for a UTF-8 one.
        assert len(ws.send.call_args.args[0]) == 3

    async def test_reader_encoding_is_configurable(self) -> None:
        """The reader decodes queued text with the consumer's encoding."""
        reader = CFWebSocketStreamReader(encoding="utf-8")
        reader.queue_message("┌")

        assert await reader.read(3) == "┌".encode()
