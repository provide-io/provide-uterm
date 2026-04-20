#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for gateway/_gateway.py and gateway/_colors.py (part 2).

Classes: TestHandleWsControlMutationKilling, TestWsToTcpMutationKilling,
         TestTcpToWsMutationKilling, TestWriteTokenMutationKilling.
"""

from __future__ import annotations

import asyncio

from provide.terminal.control_channel import encode_control
from provide.terminal.gateway._gateway import (
    _handle_ws_control,
    _read_token,
    _tcp_to_ws,
    _write_token,
    _ws_to_tcp,
)


def _async_iter(items):
    """Return an async iterator over items."""

    async def _gen():
        for item in items:
            yield item

    return _gen()


# ---------------------------------------------------------------------------
# _handle_ws_control mutation killers (mutmut_27 — unknown msg type)
# ---------------------------------------------------------------------------


class TestHandleWsControlMutationKilling:
    async def test_unknown_msg_type_returns_false(self):
        """A known JSON object with unknown type returns False.
        Kills mutmut_27 which changes the final return True to False."""
        written = []

        async def _write_fn(data: bytes) -> None:
            written.append(data)

        result = await _handle_ws_control(encode_control({"type": "unknown_type"}), None, _write_fn)
        assert result is False
        assert written == []

    async def test_non_dict_json_returns_false(self):
        """JSON array (not dict) returns False."""

        async def _write_fn(data: bytes) -> None:
            pass

        result = await _handle_ws_control("[1, 2, 3]", None, _write_fn)
        assert result is False

    async def test_json_number_returns_false(self):
        """JSON number returns False."""

        async def _write_fn(data: bytes) -> None:
            pass

        result = await _handle_ws_control("42", None, _write_fn)
        assert result is False

    async def test_session_token_missing_token_key_returns_false(self):
        """session_token without 'token' key returns False."""

        async def _write_fn(data: bytes) -> None:
            pass

        result = await _handle_ws_control(encode_control({"type": "session_token"}), None, _write_fn)
        assert result is False

    async def test_resume_ok_writes_specific_text(self):
        """resume_ok writes exactly the Session resumed message."""
        written = []

        async def _write_fn(data: bytes) -> None:
            written.append(data)

        await _handle_ws_control(encode_control({"type": "resume_ok"}), None, _write_fn)
        assert len(written) == 1
        assert written[0] == b"\r\n[Session resumed]\r\n"


# ---------------------------------------------------------------------------
# _ws_to_tcp mutation killers
# DEL→BS conversion, CRLF normalization, color mode applied
# ---------------------------------------------------------------------------


class TestWsToTcpMutationKilling:
    async def _collect_ws_to_tcp(self, messages, **kwargs):
        written = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                pass

        from asyncio import StreamWriter
        from typing import cast

        kwargs.setdefault("token_holder", [None])
        await _ws_to_tcp(_async_iter(messages), cast("StreamWriter", MockWriter()), **kwargs)
        return b"".join(written)

    async def test_del_converted_to_backspace(self):
        """0x7F DEL in input must become 0x08 BS."""
        out = await self._collect_ws_to_tcp(["\x7f"])
        assert b"\x08" in out
        assert b"\x7f" not in out

    async def test_del_in_longer_string(self):
        """DEL in the middle of text must be converted."""
        out = await self._collect_ws_to_tcp(["abc\x7fdef"])
        assert b"\x08" in out
        assert b"\x7f" not in out
        assert b"abc" in out
        assert b"def" in out

    async def test_crlf_normalization_applied(self):
        """Bare LF must be converted to CRLF."""
        out = await self._collect_ws_to_tcp(["hello\nworld"])
        assert b"hello\r\nworld" in out

    async def test_bytes_message_forwarded_directly(self):
        """bytes messages are forwarded directly (no encode needed)."""
        out = await self._collect_ws_to_tcp([b"raw bytes"])
        assert b"raw bytes" in out

    async def test_color_mode_applied_to_str(self):
        """color_mode='256' rewrites RGB in string messages."""
        msg = "\x1b[38;2;255;0;0mtext\x1b[0m"
        out = await self._collect_ws_to_tcp([msg], color_mode="256")
        assert b"38;5;196m" in out

    async def test_control_message_intercepted(self):
        """Control JSON messages are intercepted and not written to TCP."""
        out = await self._collect_ws_to_tcp([encode_control({"type": "resume_ok"})])
        assert b"Session resumed" in out  # written by write_fn

    async def test_writer_drain_called(self):
        """drain must be called after each write."""
        drains = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                pass

            async def drain(self) -> None:
                drains.append(True)

        from asyncio import StreamWriter
        from typing import cast

        await _ws_to_tcp(_async_iter(["hello"]), cast("StreamWriter", MockWriter()), token_holder=[None])
        assert len(drains) >= 1


# ---------------------------------------------------------------------------
# _tcp_to_ws mutation killers
# ---------------------------------------------------------------------------


class TestTcpToWsMutationKilling:
    async def test_4096_byte_chunk_size(self):
        """Read exactly 4096 bytes at once."""
        sent = []

        class MockWs:
            async def send(self, data: str) -> None:
                sent.append(data)

        reader = asyncio.StreamReader()
        chunk = b"x" * 4096
        reader.feed_data(chunk)
        reader.feed_eof()

        await _tcp_to_ws(reader, MockWs())
        assert len(sent) >= 1
        assert "x" * 4096 in "".join(sent)

    async def test_latin1_encoding(self):
        """Bytes are decoded as latin-1."""
        sent = []

        class MockWs:
            async def send(self, data: str) -> None:
                sent.append(data)

        reader = asyncio.StreamReader()
        reader.feed_data(bytes([0xFF, 0xFE]))  # latin-1 chars
        reader.feed_eof()

        await _tcp_to_ws(reader, MockWs())
        assert len(sent) == 1
        # \xFF and \xFE decoded as latin-1
        assert sent[0] == "\xff\xfe"

    async def test_telnet_strip_then_send(self):
        """With telnet=True, IAC sequences stripped before sending."""
        sent = []

        class MockWs:
            async def send(self, data: str) -> None:
                sent.append(data)

        reader = asyncio.StreamReader()
        reader.feed_data(bytes([255, 251, 1]) + b"payload")  # IAC WILL ECHO + payload
        reader.feed_eof()

        await _tcp_to_ws(reader, MockWs(), telnet=True)
        assert len(sent) == 1
        assert sent[0] == "payload"

    async def test_telnet_all_iac_empty_skips_send(self):
        """If all data is IAC after stripping, nothing sent."""
        sent = []

        class MockWs:
            async def send(self, data: str) -> None:
                sent.append(data)

        reader = asyncio.StreamReader()
        reader.feed_data(bytes([255, 251, 1]))  # IAC only
        reader.feed_eof()

        await _tcp_to_ws(reader, MockWs(), telnet=True)
        assert sent == []


# ---------------------------------------------------------------------------
# _write_token mutation killers (parents=True, exist_ok=True are required)
# ---------------------------------------------------------------------------


class TestWriteTokenMutationKilling:
    def test_creates_nested_directory(self, tmp_path):
        """parents=True must create nested dirs (mutmut_3 removes parents arg)."""
        p = tmp_path / "a" / "b" / "c" / "token"
        _write_token(p, "val")
        assert p.read_text() == "val"

    def test_existing_dir_does_not_raise(self, tmp_path):
        """exist_ok=True prevents FileExistsError (mutmut_4 removes exist_ok)."""
        p = tmp_path / "token"
        # Write twice — second call should not raise even though dir exists
        _write_token(p, "first")
        _write_token(p, "second")
        assert p.read_text() == "second"

    def test_overwrites_existing_token(self, tmp_path):
        """Writing to same path overwrites correctly."""
        p = tmp_path / "tok"
        _write_token(p, "original")
        _write_token(p, "updated")
        assert _read_token(p) == "updated"
