#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.gateway — pump helpers (tcp↔ws) and TelnetWsGateway."""

from __future__ import annotations

import asyncio
from typing import Any

import websockets
import websockets.server
from provide.uterm.control_channel import (
    ControlChannelDecoder,
    ControlChunk,
    encode_control,
)
from provide.uterm.gateway import (
    _normalize_crlf,
    _pipe_ws,
    _ws_to_tcp,
)

# ---------------------------------------------------------------------------
# Pump helper unit tests
# ---------------------------------------------------------------------------


def _decode_control(raw: str) -> dict[str, Any]:
    decoder = ControlChannelDecoder()
    events = decoder.feed(raw)
    events.extend(decoder.finish())
    assert len(events) == 1
    assert isinstance(events[0], ControlChunk)
    return events[0].control


def _async_iter(items):
    async def _gen():
        for item in items:
            yield item

    return _gen()


class TestWsToTcpResume:
    async def test_session_token_intercepted_not_forwarded(self) -> None:
        token_holder: list[dict | None] = [None]
        written: list[bytes] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                pass

        from asyncio import StreamWriter
        from typing import cast

        msg = encode_control({"type": "session_token", "token": "tok123"})
        await _ws_to_tcp(_async_iter([msg]), cast("StreamWriter", MockWriter()), token_holder=token_holder)
        assert written == []
        assert token_holder[0] is not None
        assert token_holder[0]["token"] == "tok123"

    async def test_resume_ok_sends_text_to_tcp(self) -> None:
        written: list[bytes] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                pass

        from asyncio import StreamWriter
        from typing import cast

        await _ws_to_tcp(
            _async_iter([encode_control({"type": "resume_ok"})]),
            cast("StreamWriter", MockWriter()),
            token_holder=[None],
        )
        assert any(b"Session resumed" in w for w in written)

    async def test_resume_failed_clears_token_holder(self) -> None:
        token_holder: list[dict | None] = [{"token": "old"}]
        written: list[bytes] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                pass

        from asyncio import StreamWriter
        from typing import cast

        await _ws_to_tcp(
            _async_iter([encode_control({"type": "resume_failed"})]),
            cast("StreamWriter", MockWriter()),
            token_holder=token_holder,
        )
        assert token_holder[0] is None

    async def test_plain_text_forwarded(self) -> None:
        written: list[bytes] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                pass

        from asyncio import StreamWriter
        from typing import cast

        await _ws_to_tcp(_async_iter(["hello"]), cast("StreamWriter", MockWriter()), token_holder=[None])
        assert b"hello" in written[0]


# ---------------------------------------------------------------------------
# _ws_to_tcp — CRLF normalization
# ---------------------------------------------------------------------------


class TestWsToTcpCrlf:
    async def test_bare_lf_becomes_crlf(self) -> None:
        written: list[bytes] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                pass

        from asyncio import StreamWriter
        from typing import cast

        await _ws_to_tcp(_async_iter(["foo\nbar"]), cast("StreamWriter", MockWriter()), token_holder=[None])
        assert written[0] == b"foo\r\nbar"

    async def test_existing_crlf_not_doubled(self) -> None:
        written: list[bytes] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                pass

        from asyncio import StreamWriter
        from typing import cast

        await _ws_to_tcp(_async_iter(["foo\r\nbar"]), cast("StreamWriter", MockWriter()), token_holder=[None])
        assert written[0] == b"foo\r\nbar"


# ---------------------------------------------------------------------------
# _normalize_crlf unit tests
# ---------------------------------------------------------------------------


class TestNormalizeCrlf:
    def test_bare_lf_converted(self) -> None:
        assert _normalize_crlf(b"a\nb") == b"a\r\nb"

    def test_crlf_not_doubled(self) -> None:
        assert _normalize_crlf(b"a\r\nb") == b"a\r\nb"

    def test_no_newline_unchanged(self) -> None:
        assert _normalize_crlf(b"hello") == b"hello"


# ---------------------------------------------------------------------------
# _pipe_ws — resume token
# ---------------------------------------------------------------------------


class TestPipeWsResume:
    async def test_token_holder_present_sends_resume(self, tmp_path) -> None:
        """When a token_holder contains a token dict, the first WS message should be an encoded resume control frame."""
        received: list[str] = []
        first_message = asyncio.Event()

        async def handler(ws: websockets.ServerConnection) -> None:
            try:
                async for msg in ws:
                    received.append(msg if isinstance(msg, str) else msg.decode())
                    first_message.set()
            except websockets.exceptions.ConnectionClosed:
                pass

        srv = await websockets.serve(handler, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            token_holder: list[dict | None] = [{"token": "resume_tok_abc"}]

            reader = asyncio.StreamReader()
            reader.feed_eof()

            class MockWriter:
                def write(self, data: bytes) -> None:
                    pass

                async def drain(self) -> None:
                    pass

                def close(self) -> None:
                    pass

                async def wait_closed(self) -> None:
                    pass

            from asyncio import StreamWriter
            from typing import cast

            await asyncio.wait_for(
                _pipe_ws(
                    reader,
                    cast("StreamWriter", MockWriter()),
                    f"ws://127.0.0.1:{port}",
                    token_holder=token_holder,
                ),
                timeout=3.0,
            )
            # Wait for the server to actually process the first frame — the
            # client closes its side as soon as the EOF reader task wins, so
            # the server-side handler may still be draining.
            await asyncio.wait_for(first_message.wait(), timeout=2.0)
        finally:
            srv.close()
            await srv.wait_closed()

        assert len(received) >= 1
        first = _decode_control(received[0])
        assert first == {"type": "resume", "token": "resume_tok_abc"}

    async def test_no_token_holder_sends_no_resume(self) -> None:
        """When token_holder is [None], no resume message is sent."""
        received: list[str] = []

        async def handler(ws: websockets.ServerConnection) -> None:
            received.extend([msg if isinstance(msg, str) else msg.decode() async for msg in ws])

        srv = await websockets.serve(handler, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            reader = asyncio.StreamReader()
            reader.feed_eof()

            class MockWriter:
                def write(self, data: bytes) -> None:
                    pass

                async def drain(self) -> None:
                    pass

                def close(self) -> None:
                    pass

                async def wait_closed(self) -> None:
                    pass

            from asyncio import StreamWriter
            from typing import cast

            await asyncio.wait_for(
                _pipe_ws(
                    reader,
                    cast("StreamWriter", MockWriter()),
                    f"ws://127.0.0.1:{port}",
                    token_holder=[None],
                ),
                timeout=3.0,
            )
        finally:
            srv.close()

        assert received == []
