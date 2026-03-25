#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.gateway — token helpers, WS control, IAC stripping, color helpers."""

from __future__ import annotations

import asyncio
from typing import Any

import websockets

from provide.terminal.control_channel import encode_control
from provide.terminal.gateway import (
    TelnetWsGateway,
    _apply_color_mode,
    _clamp8,
    _delete_token,
    _handle_ws_control,
    _read_token,
    _rgb_to_256,
    _strip_iac,
    _tcp_to_ws,
    _write_token,
    _ws_to_tcp,
)


def _async_iter(items):
    """Return an async iterator over *items*."""

    async def _gen():
        for item in items:
            yield item

    return _gen()


# ---------------------------------------------------------------------------
# Token file helpers
# ---------------------------------------------------------------------------


class TestTokenFileHelpers:
    def test_read_missing_returns_none(self, tmp_path) -> None:
        assert _read_token(tmp_path / "no_such_file") is None

    def test_write_then_read(self, tmp_path) -> None:
        p = tmp_path / "token"
        _write_token(p, "mytoken")
        assert _read_token(p) == "mytoken"

    def test_write_empty_returns_none(self, tmp_path) -> None:
        p = tmp_path / "token"
        _write_token(p, "")
        assert _read_token(p) is None

    def test_write_creates_parent_dirs(self, tmp_path) -> None:
        p = tmp_path / "nested" / "dir" / "token"
        _write_token(p, "abc")
        assert p.read_text() == "abc"

    def test_delete_existing(self, tmp_path) -> None:
        p = tmp_path / "token"
        p.write_text("x")
        _delete_token(p)
        assert not p.exists()

    def test_delete_missing_no_raise(self, tmp_path) -> None:
        _delete_token(tmp_path / "no_such_file")  # must not raise


# ---------------------------------------------------------------------------
# JSON control message handler
# ---------------------------------------------------------------------------


class TestHandleWsControl:
    async def test_session_token_saves_file_and_returns_true(self, tmp_path) -> None:
        token_file = tmp_path / "tok"
        written: list[bytes] = []

        async def _write_fn(data: bytes) -> None:
            written.append(data)

        result = await _handle_ws_control(
            encode_control({"type": "session_token", "token": "abc123"}), token_file, _write_fn
        )
        assert result is True
        assert token_file.read_text() == "abc123"
        assert written == []

    async def test_resume_ok_writes_text_and_returns_true(self) -> None:
        written: list[bytes] = []

        async def _write_fn(data: bytes) -> None:
            written.append(data)

        result = await _handle_ws_control(encode_control({"type": "resume_ok"}), None, _write_fn)
        assert result is True
        assert b"Session resumed" in written[0]

    async def test_resume_failed_deletes_token_and_returns_true(self, tmp_path) -> None:
        token_file = tmp_path / "tok"
        token_file.write_text("old_token")
        written: list[bytes] = []

        async def _write_fn(data: bytes) -> None:
            written.append(data)

        result = await _handle_ws_control(encode_control({"type": "resume_failed"}), token_file, _write_fn)
        assert result is True
        assert not token_file.exists()

    async def test_plain_text_returns_false(self) -> None:
        async def _write_fn(data: bytes) -> None:
            pass

        assert await _handle_ws_control("hello world", None, _write_fn) is False

    async def test_malformed_json_returns_false(self) -> None:
        async def _write_fn(data: bytes) -> None:
            pass

        assert await _handle_ws_control("{not json}", None, _write_fn) is False

    async def test_no_token_file_skips_write(self) -> None:
        """session_token with token_file=None should not write or raise."""

        async def _write_fn(data: bytes) -> None:
            pass

        result = await _handle_ws_control(encode_control({"type": "session_token", "token": "x"}), None, _write_fn)
        assert result is False  # token_file is None → skip write

    async def test_resume_failed_no_token_file_no_raise(self) -> None:
        async def _write_fn(data: bytes) -> None:
            pass

        result = await _handle_ws_control(encode_control({"type": "resume_failed"}), None, _write_fn)
        assert result is True


# ---------------------------------------------------------------------------
# IAC stripping
# ---------------------------------------------------------------------------


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


class TestIacStripping:
    def test_plain_data_unchanged(self) -> None:
        assert _strip_iac(b"hello") == b"hello"

    def test_iac_will_stripped(self) -> None:
        assert _strip_iac(bytes([255, 251, 1]) + b"hi") == b"hi"

    def test_iac_do_stripped(self) -> None:
        assert _strip_iac(bytes([255, 253, 3]) + b"x") == b"x"

    def test_iac_sb_stripped(self) -> None:
        data = bytes([255, 250, 1, 2, 3, 255, 240]) + b"after"
        assert _strip_iac(data) == b"after"

    def test_escaped_iac_preserved(self) -> None:
        assert _strip_iac(bytes([255, 255])) == bytes([255])

    def test_ip_maps_to_ctrl_c(self) -> None:
        assert _strip_iac(bytes([255, 244])) == bytes([0x03])

    def test_eof_maps_to_ctrl_d(self) -> None:
        assert _strip_iac(bytes([255, 236])) == bytes([0x04])

    async def test_telnet_true_strips_iac_in_tcp_to_ws(self) -> None:
        sent: list[str] = []

        class MockWs:
            async def send(self, data: str) -> None:
                sent.append(data)

        reader = asyncio.StreamReader()
        reader.feed_data(bytes([255, 251, 1]) + b"hi")
        reader.feed_eof()

        await _tcp_to_ws(reader, MockWs(), telnet=True)
        assert sent == ["hi"]

    async def test_telnet_ws_gateway_strips_iac_by_default(self) -> None:
        """TelnetWsGateway strips IAC automatically (telnet=True wired in _handle).

        Send IAC WILL ECHO + "ping" from the telnet client. The echo server
        reflects whatever it receives. If IAC is stripped, the echo arrives
        as "ping" with no 0xFF bytes.
        """
        ws_srv, ws_port = await _start_ws_echo_server()
        try:
            gw = TelnetWsGateway(f"ws://127.0.0.1:{ws_port}")
            tcp_srv = await gw.start("127.0.0.1", 0)
            from asyncio import Server

            assert isinstance(tcp_srv, Server)
            assert tcp_srv.sockets is not None
            tcp_port = tcp_srv.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
            writer.write(bytes([255, 251, 1]) + b"ping")  # IAC WILL ECHO + "ping"
            await writer.drain()
            data = await asyncio.wait_for(reader.read(256), timeout=2.0)
            writer.close()
            tcp_srv.close()
        finally:
            ws_srv.close()

        # IAC stripped → WS echoed "ping" → telnet gets "ping" back, no 0xFF
        assert b"ping" in data
        assert b"\xff" not in data

    async def test_telnet_false_does_not_strip(self) -> None:
        sent: list[str] = []

        class MockWs:
            async def send(self, data: str) -> None:
                sent.append(data)

        reader = asyncio.StreamReader()
        reader.feed_data(bytes([255, 251, 1]) + b"hi")
        reader.feed_eof()

        await _tcp_to_ws(reader, MockWs(), telnet=False)
        assert sent[0] == bytes([255, 251, 1]).decode("latin-1") + "hi"

    def test_trailing_iac_only_discarded(self) -> None:
        """Bare IAC at end of buffer (no following byte) is silently dropped."""
        assert _strip_iac(bytes([255])) == b""

    def test_iac_sb_unterminated_discards_rest(self) -> None:
        """SB subnegotiation without IAC SE: all data consumed, nothing emitted."""
        assert _strip_iac(bytes([255, 250, 1, 2, 3])) == b""

    def test_iac_ao_discarded(self) -> None:
        """IAC AO (abort output) is silently dropped."""
        assert _strip_iac(bytes([255, 245])) == b""

    def test_iac_will_truncated_discarded(self) -> None:
        """IAC WILL without option byte (truncated): silently dropped."""
        assert _strip_iac(bytes([255, 251])) == b""

    def test_iac_unknown_cmd_discarded(self) -> None:
        """Unknown IAC command is consumed (2 bytes) without emitting data."""
        assert _strip_iac(bytes([255, 200])) == b""

    async def test_all_iac_stripped_to_empty_is_skipped(self) -> None:
        """If telnet=True strips all IAC leaving empty bytes, the WS send is skipped."""
        sent: list[str] = []

        class MockWs:
            async def send(self, data: str) -> None:
                sent.append(data)

        reader = asyncio.StreamReader()
        reader.feed_data(bytes([255, 251, 1]))  # IAC WILL ECHO — strips to b""
        reader.feed_eof()

        await _tcp_to_ws(reader, MockWs(), telnet=True)
        assert sent == []  # nothing sent because stripped result was empty


# ---------------------------------------------------------------------------
# _ws_to_tcp — color mode
# ---------------------------------------------------------------------------


class TestWsToTcpColorMode:
    async def _collect(self, messages: list, **kwargs) -> bytes:
        written: list[bytes] = []

        class MockWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                pass

        from asyncio import StreamWriter
        from typing import cast

        await _ws_to_tcp(_async_iter(messages), cast("StreamWriter", MockWriter()), **kwargs)
        return b"".join(written)

    async def test_passthrough_keeps_rgb(self) -> None:
        msg = "\x1b[38;2;10;20;30mtext\x1b[0m"
        out = await self._collect([msg], color_mode="passthrough")
        assert b"38;2;10;20;30m" in out

    async def test_256_transforms_rgb(self) -> None:
        msg = "\x1b[38;2;10;20;30mtext\x1b[0m"
        out = await self._collect([msg], color_mode="256")
        assert b"38;2;" not in out
        assert b"38;5;" in out

    async def test_16_transforms_rgb(self) -> None:
        msg = "\x1b[38;2;10;20;30mtext\x1b[0m"
        out = await self._collect([msg], color_mode="16")
        assert b"38;2;" not in out
        assert b"38;5;" not in out


# ---------------------------------------------------------------------------
# _apply_color_mode unit tests
# ---------------------------------------------------------------------------


class TestApplyColorMode:
    def test_passthrough_unchanged(self) -> None:
        raw = b"\x1b[38;2;10;20;30mtext\x1b[0m"
        assert _apply_color_mode(raw, "passthrough") == raw

    def test_256_rewrites_rgb_fg(self) -> None:
        raw = b"\x1b[38;2;10;20;30mFG\x1b[0m"
        out = _apply_color_mode(raw, "256")
        assert b"38;2;" not in out
        assert b"38;5;" in out

    def test_256_rewrites_rgb_bg(self) -> None:
        raw = b"\x1b[48;2;200;180;40mBG\x1b[0m"
        out = _apply_color_mode(raw, "256")
        assert b"48;2;" not in out
        assert b"48;5;" in out

    def test_16_rewrites_rgb_fg(self) -> None:
        raw = b"\x1b[38;2;10;20;30mFG\x1b[0m"
        out = _apply_color_mode(raw, "16")
        assert b"38;2;" not in out
        assert b"38;5;" not in out

    def test_16_rewrites_rgb_bg(self) -> None:
        raw = b"\x1b[48;2;200;180;40mBG\x1b[0m"
        out = _apply_color_mode(raw, "16")
        assert b"48;2;" not in out
        assert b"48;5;" not in out


# ---------------------------------------------------------------------------
# _rgb_to_256 grayscale paths
# ---------------------------------------------------------------------------


class TestRgbTo256Grayscale:
    def test_near_black_returns_16(self) -> None:
        assert _rgb_to_256(4, 4, 4) == 16

    def test_near_white_returns_231(self) -> None:
        assert _rgb_to_256(250, 250, 250) == 231

    def test_mid_gray_returns_grayscale_ramp(self) -> None:
        result = _rgb_to_256(128, 128, 128)
        assert 232 <= result <= 255


# ---------------------------------------------------------------------------
# _apply_color_mode — empty SGR params path
# ---------------------------------------------------------------------------


class TestApplyColorModeEdge:
    def test_empty_sgr_params_unchanged(self) -> None:
        """ESC[m (reset — empty params) passes through unmodified."""
        raw = b"\x1b[mtext"
        assert _apply_color_mode(raw, "256") == raw

    def test_clamp8_below_zero(self) -> None:
        assert _clamp8(-5) == 0

    def test_clamp8_above_255(self) -> None:
        assert _clamp8(300) == 255
