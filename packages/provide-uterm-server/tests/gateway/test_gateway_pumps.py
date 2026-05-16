#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for gateway pump helpers: _tcp_to_ws, _ws_to_tcp, _pipe_ws, _ssh_to_ws, _ws_to_ssh."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from provide.uterm.control_channel import encode_control, encode_data
from provide.uterm.gateway._gateway import (
    _pipe_ws,
    _ssh_to_ws,
    _tcp_to_ws,
    _ws_to_ssh,
    _ws_to_tcp,
)

# ---------------------------------------------------------------------------
# Async iterator helper for mocking `async for message in ws`
# ---------------------------------------------------------------------------


class _AsyncIter:
    """Wrap a list of items into an async iterator."""

    def __init__(self, items: list[Any]) -> None:
        self._items = iter(items)

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None


def _mock_ws(messages: list[Any]) -> MagicMock:
    """Create a mock WS that yields *messages* via ``async for``."""
    ws = MagicMock()
    ws.__aiter__ = lambda self: _AsyncIter(messages)
    ws.send = AsyncMock()
    return ws


def _make_ws_context(ws_mock: MagicMock) -> MagicMock:
    """Build a fake websockets.connect() async context manager."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=ws_mock)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# _tcp_to_ws
# ---------------------------------------------------------------------------


class TestTcpToWs:
    async def test_forwards_data(self) -> None:
        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(side_effect=[b"hello", b""])
        ws = AsyncMock()
        await _tcp_to_ws(reader, ws, telnet=False)
        assert ws.send.call_count == 1

    async def test_strips_iac_when_telnet(self) -> None:
        reader = AsyncMock(spec=asyncio.StreamReader)
        data = bytes([0xFF, 0xFB, 0x01]) + b"X"
        reader.read = AsyncMock(side_effect=[data, b""])
        ws = AsyncMock()
        await _tcp_to_ws(reader, ws, telnet=True)
        assert ws.send.call_count == 1
        sent = ws.send.call_args[0][0]
        assert "X" in sent

    async def test_skips_empty_after_iac_strip(self) -> None:
        reader = AsyncMock(spec=asyncio.StreamReader)
        data = bytes([0xFF, 0xFB, 0x01])
        reader.read = AsyncMock(side_effect=[data, b""])
        ws = AsyncMock()
        await _tcp_to_ws(reader, ws, telnet=True)
        assert ws.send.call_count == 0


# ---------------------------------------------------------------------------
# _ws_to_tcp
# ---------------------------------------------------------------------------


class TestWsToTcp:
    async def test_forwards_text_messages(self) -> None:
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.drain = AsyncMock()
        msg = encode_data("hi")
        ws = _mock_ws([msg])
        await _ws_to_tcp(ws, writer, token_holder=[None], color_mode="passthrough")
        assert writer.write.called

    async def test_forwards_binary_messages(self) -> None:
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.drain = AsyncMock()
        ws = _mock_ws([b"hello\n"])
        await _ws_to_tcp(ws, writer, token_holder=[None], color_mode="passthrough")
        written = writer.write.call_args[0][0]
        assert b"\r\n" in written

    async def test_del_to_bs_conversion(self) -> None:
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.drain = AsyncMock()
        ws = _mock_ws([b"\x7f"])
        await _ws_to_tcp(ws, writer, token_holder=[None], color_mode="passthrough")
        written = writer.write.call_args[0][0]
        assert b"\x08" in written

    async def test_control_message_handled(self) -> None:
        holder: list[dict | None] = [None]
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.drain = AsyncMock()
        msg = encode_control({"type": "session_token", "token": "t1"})
        ws = _mock_ws([msg])
        await _ws_to_tcp(ws, writer, token_holder=holder, color_mode="passthrough")
        assert holder[0] == {"token": "t1"}

    async def test_protocol_error_skipped(self) -> None:
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.drain = AsyncMock()
        bad_msg = "\x10\x02" + "000000xx:bad"
        ws = _mock_ws([bad_msg])
        await _ws_to_tcp(ws, writer, token_holder=[None], color_mode="passthrough")

    async def test_color_mode_applied(self) -> None:
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.drain = AsyncMock()
        msg = encode_data("\x1b[38;2;255;0;0mRed")
        ws = _mock_ws([msg])
        await _ws_to_tcp(ws, writer, token_holder=[None], color_mode="256")
        written = writer.write.call_args[0][0]
        assert b"38;5;" in written

    async def test_del_to_bs_in_text_data(self) -> None:
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.drain = AsyncMock()
        msg = encode_data("\x7f")
        ws = _mock_ws([msg])
        await _ws_to_tcp(ws, writer, token_holder=[None], color_mode="passthrough")
        written = writer.write.call_args[0][0]
        assert b"\x08" in written

    async def test_resume_ok_calls_write_fn(self) -> None:
        """Cover the _write_fn closure (lines 237-238)."""
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.drain = AsyncMock()
        msg = encode_control({"type": "resume_ok"})
        ws = _mock_ws([msg])
        await _ws_to_tcp(ws, writer, token_holder=[None], color_mode="passthrough")
        writer.write.assert_called_once_with(b"\r\n[Session resumed]\r\n")
        writer.drain.assert_called()


# ---------------------------------------------------------------------------
# _pipe_ws
# ---------------------------------------------------------------------------


class TestPipeWs:
    async def test_opens_ws_and_pipes(self) -> None:
        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(return_value=b"")
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.drain = AsyncMock()

        ws_mock = _mock_ws([])

        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await _pipe_ws(reader, writer, "ws://test", token_holder=[None], telnet=False)

    async def test_sends_resume_token(self) -> None:
        holder: list[dict | None] = [{"token": "mytoken"}]

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(return_value=b"")
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.drain = AsyncMock()

        ws_mock = _mock_ws([])

        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await _pipe_ws(reader, writer, "ws://test", token_holder=holder, telnet=False)
            first_send = ws_mock.send.call_args_list[0][0][0]
            assert "resume" in first_send
            assert "mytoken" in first_send

    async def test_sends_resume_token_with_player_id(self) -> None:
        """Cover player_id included in resume when token has player_id."""
        holder: list[dict | None] = [{"token": "mytoken", "player_id": 7}]

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(return_value=b"")
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.drain = AsyncMock()

        ws_mock = _mock_ws([])
        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await _pipe_ws(reader, writer, "ws://test", token_holder=holder, telnet=False)
            first_send = ws_mock.send.call_args_list[0][0][0]
            assert "player_id" in first_send
            assert "7" in first_send

    async def test_no_resume_without_token(self) -> None:
        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(return_value=b"")
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.drain = AsyncMock()

        ws_mock = _mock_ws([])

        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await _pipe_ws(reader, writer, "ws://test", token_holder=[None], telnet=False)

    async def test_cancels_pending_task(self) -> None:
        """Cover line 285: task.cancel() when one pump finishes first."""

        async def slow_read(_n: int = 4096) -> bytes:
            await asyncio.sleep(100)
            return b""

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = slow_read
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.drain = AsyncMock()

        msg = encode_data("x")
        ws_mock = _mock_ws([msg])

        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await _pipe_ws(reader, writer, "ws://test", token_holder=[None], telnet=False)


class TestPipeWsIacNegotiate:
    """Covers the pre-WS TTYPE/NEW-ENVIRON window in _pipe_ws."""

    async def test_ttype_reply_appends_colormode(self) -> None:
        """Client replies with TTYPE IS xterm-256color → ws_url gains ?colormode=256."""
        IAC, SB, SE, SUB_IS, TTYPE = 255, 250, 240, 0, 24
        ttype_reply = bytes([IAC, SB, TTYPE, SUB_IS]) + b"xterm-256color" + bytes([IAC, SE])
        # Also advertise both options so done() returns True quickly.
        new_environ_empty = bytes([IAC, SB, 39, SUB_IS, IAC, SE])

        reader = AsyncMock(spec=asyncio.StreamReader)
        # Deliver TTYPE + NEW-ENVIRON subnegotiations, then EOF.
        reader.read = AsyncMock(side_effect=[ttype_reply + new_environ_empty, b""])
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.drain = AsyncMock()

        ws_mock = _mock_ws([])
        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await _pipe_ws(
                reader,
                writer,
                "ws://test",
                token_holder=[None],
                telnet=True,
                iac_negotiate=True,
                iac_negotiate_timeout=0.2,
            )

        # The URL passed to websockets.connect should carry ?colormode=256.
        connected_url = mock_ws_mod.connect.call_args[0][0]
        assert connected_url.startswith("ws://test")
        assert "colormode=256" in connected_url

    async def test_no_client_reply_leaves_url_untouched(self) -> None:
        """Silent client → negotiation times out cleanly, ws_url unchanged."""
        reader = AsyncMock(spec=asyncio.StreamReader)
        # Simulate a silent client: read() returns nothing before deadline.
        reader.read = AsyncMock(side_effect=TimeoutError)
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.drain = AsyncMock()

        ws_mock = _mock_ws([])
        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await _pipe_ws(
                reader,
                writer,
                "ws://test",
                token_holder=[None],
                telnet=True,
                iac_negotiate=True,
                iac_negotiate_timeout=0.01,
            )

        connected_url = mock_ws_mod.connect.call_args[0][0]
        assert connected_url == "ws://test"

    async def test_disabled_when_not_telnet(self) -> None:
        """iac_negotiate=True + telnet=False → skip negotiation entirely."""
        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(return_value=b"")
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.drain = AsyncMock()

        ws_mock = _mock_ws([])
        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await _pipe_ws(
                reader,
                writer,
                "ws://test",
                token_holder=[None],
                telnet=False,  # <-- the gate
                iac_negotiate=True,
                iac_negotiate_timeout=0.01,
            )

        # start_bytes() should never have been written to the client.
        writer.write.assert_not_called()

    async def test_existing_query_string_uses_ampersand(self) -> None:
        """ws_url already has ?x=1 → appended param uses &, not ?."""
        IAC, SB, SE, SUB_IS, TTYPE = 255, 250, 240, 0, 24
        ttype_reply = bytes([IAC, SB, TTYPE, SUB_IS]) + b"xterm-256color" + bytes([IAC, SE])
        new_environ_empty = bytes([IAC, SB, 39, SUB_IS, IAC, SE])

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(side_effect=[ttype_reply + new_environ_empty, b""])
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.drain = AsyncMock()

        ws_mock = _mock_ws([])
        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await _pipe_ws(
                reader,
                writer,
                "ws://test?token=abc",
                token_holder=[None],
                telnet=True,
                iac_negotiate=True,
                iac_negotiate_timeout=0.2,
            )

        connected_url = mock_ws_mod.connect.call_args[0][0]
        assert connected_url == "ws://test?token=abc&colormode=256"


# ---------------------------------------------------------------------------
# _ssh_to_ws
# ---------------------------------------------------------------------------


class TestSshToWs:
    async def test_forwards_string_data(self) -> None:
        process = MagicMock()
        process.stdin = AsyncMock()
        process.stdin.read = AsyncMock(side_effect=["hello", ""])
        ws = AsyncMock()
        await _ssh_to_ws(process, ws)
        assert ws.send.call_count == 1

    async def test_forwards_bytes_data(self) -> None:
        process = MagicMock()
        process.stdin = AsyncMock()
        process.stdin.read = AsyncMock(side_effect=[b"hello", b""])
        ws = AsyncMock()
        await _ssh_to_ws(process, ws)
        assert ws.send.call_count == 1

    async def test_breaks_on_exception(self) -> None:
        process = MagicMock()
        process.stdin = AsyncMock()
        process.stdin.read = AsyncMock(side_effect=OSError("broken"))
        ws = AsyncMock()
        await _ssh_to_ws(process, ws)
        assert ws.send.call_count == 0

    async def test_breaks_on_none(self) -> None:
        process = MagicMock()
        process.stdin = AsyncMock()
        process.stdin.read = AsyncMock(return_value=None)
        ws = AsyncMock()
        await _ssh_to_ws(process, ws)
        assert ws.send.call_count == 0


# ---------------------------------------------------------------------------
# _ws_to_ssh
# ---------------------------------------------------------------------------


class TestWsToSsh:
    async def test_forwards_text_messages(self) -> None:
        process = MagicMock()
        process.stdout = MagicMock()
        msg = encode_data("hi")
        ws = _mock_ws([msg])
        await _ws_to_ssh(ws, process, token_holder=[None], color_mode="passthrough")
        assert process.stdout.write.called

    async def test_forwards_binary_messages(self) -> None:
        process = MagicMock()
        process.stdout = MagicMock()
        ws = _mock_ws([b"hello"])
        await _ws_to_ssh(ws, process, token_holder=[None], color_mode="passthrough")
        assert process.stdout.write.called

    async def test_handles_control_message(self) -> None:
        holder: list[dict | None] = [None]
        process = MagicMock()
        process.stdout = MagicMock()
        msg = encode_control({"type": "session_token", "token": "t2"})
        ws = _mock_ws([msg])
        await _ws_to_ssh(ws, process, token_holder=holder, color_mode="passthrough")
        assert holder[0] == {"token": "t2"}

    async def test_protocol_error_skipped(self) -> None:
        process = MagicMock()
        process.stdout = MagicMock()
        bad_msg = "\x10\x02" + "000000xx:bad"
        ws = _mock_ws([bad_msg])
        await _ws_to_ssh(ws, process, token_holder=[None], color_mode="passthrough")

    async def test_color_mode_applied(self) -> None:
        process = MagicMock()
        process.stdout = MagicMock()
        msg = encode_data("\x1b[38;2;255;0;0mRed")
        ws = _mock_ws([msg])
        await _ws_to_ssh(ws, process, token_holder=[None], color_mode="256")
        written = process.stdout.write.call_args[0][0]
        assert "38;5;" in written

    async def test_color_mode_applied_binary(self) -> None:
        process = MagicMock()
        process.stdout = MagicMock()
        ws = _mock_ws([b"\x1b[38;2;255;0;0mRed"])
        await _ws_to_ssh(ws, process, token_holder=[None], color_mode="256")
        written = process.stdout.write.call_args[0][0]
        assert "38;5;" in written

    async def test_resume_ok_calls_write_fn(self) -> None:
        """Cover the _write_fn closure (line 320)."""
        process = MagicMock()
        process.stdout = MagicMock()
        msg = encode_control({"type": "resume_ok"})
        ws = _mock_ws([msg])
        await _ws_to_ssh(ws, process, token_holder=[None], color_mode="passthrough")
        process.stdout.write.assert_called_once()
        written = process.stdout.write.call_args[0][0]
        assert "Session resumed" in written
