#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.gateway._gateway — non-pump helper functions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from provide.terminal.control_channel import encode_control, encode_data
from provide.terminal.gateway._gateway import (
    _delete_token,
    _handle_ws_control,
    _handle_ws_control_frame,
    _make_no_auth_server_class,
    _normalize_crlf,
    _read_token,
    _require_websockets,
    _skip_subneg_sequence,
    _strip_iac,
    _write_token,
)

# ---------------------------------------------------------------------------
# Token file helpers
# ---------------------------------------------------------------------------


class TestReadToken:
    def test_reads_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "token"
        f.write_text("  abc123  ")
        assert _read_token(f) == "abc123"

    def test_returns_none_for_missing(self, tmp_path: Path) -> None:
        assert _read_token(tmp_path / "nonexistent") is None

    def test_returns_none_for_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "token"
        f.write_text("   ")
        assert _read_token(f) is None


class TestWriteToken:
    def test_writes_and_creates_parents(self, tmp_path: Path) -> None:
        f = tmp_path / "sub" / "dir" / "token"
        _write_token(f, "mytoken")
        assert f.read_text() == "mytoken"


class TestDeleteToken:
    def test_deletes_existing(self, tmp_path: Path) -> None:
        f = tmp_path / "token"
        f.write_text("x")
        _delete_token(f)
        assert not f.exists()

    def test_noop_if_missing(self, tmp_path: Path) -> None:
        _delete_token(tmp_path / "nope")  # should not raise


# ---------------------------------------------------------------------------
# CRLF normalization
# ---------------------------------------------------------------------------


class TestNormalizeCrlf:
    def test_bare_lf_converted(self) -> None:
        assert _normalize_crlf(b"a\nb") == b"a\r\nb"

    def test_existing_crlf_preserved(self) -> None:
        assert _normalize_crlf(b"a\r\nb") == b"a\r\nb"

    def test_no_newlines(self) -> None:
        assert _normalize_crlf(b"hello") == b"hello"

    def test_mixed(self) -> None:
        assert _normalize_crlf(b"a\r\nb\nc") == b"a\r\nb\r\nc"


# ---------------------------------------------------------------------------
# IAC stripping
# ---------------------------------------------------------------------------


class TestSkipSubnegSequence:
    def test_finds_iac_se(self) -> None:
        data = bytes([0x01, 0xFF, 0xF0, 0x42])
        assert _skip_subneg_sequence(data, 0, len(data)) == 3

    def test_truncated_returns_n(self) -> None:
        data = bytes([0x01, 0x02])
        assert _skip_subneg_sequence(data, 0, len(data)) == 2


class TestStripIac:
    def test_plain_data_unchanged(self) -> None:
        assert _strip_iac(b"hello") == b"hello"

    def test_double_iac_becomes_single(self) -> None:
        assert _strip_iac(bytes([0xFF, 0xFF])) == bytes([0xFF])

    def test_will_do_wont_dont_stripped(self) -> None:
        for cmd in (251, 252, 253, 254):  # WILL, WONT, DO, DONT
            data = bytes([0xFF, cmd, 0x01, 0x41])
            assert _strip_iac(data) == b"A"

    def test_subneg_stripped(self) -> None:
        data = bytes([0xFF, 0xFA, 0x01, 0x02, 0xFF, 0xF0, 0x41])
        assert _strip_iac(data) == b"A"

    def test_ip_becomes_ctrl_c(self) -> None:
        assert _strip_iac(bytes([0xFF, 0xF4])) == bytes([0x03])

    def test_break_becomes_ctrl_c(self) -> None:
        assert _strip_iac(bytes([0xFF, 0xF3])) == bytes([0x03])

    def test_eof_becomes_ctrl_d(self) -> None:
        assert _strip_iac(bytes([0xFF, 0xEC])) == bytes([0x04])

    def test_unknown_command_skipped(self) -> None:
        data = bytes([0xFF, 0xF5, 0x41])
        assert _strip_iac(data) == b"A"

    def test_truncated_iac_at_end(self) -> None:
        assert _strip_iac(bytes([0x41, 0xFF])) == b"A"

    def test_truncated_will_at_end(self) -> None:
        assert _strip_iac(bytes([0x41, 0xFF, 0xFB])) == b"A"

    def test_empty_after_iac_strip(self) -> None:
        assert _strip_iac(bytes([0xFF, 0xFB, 0x01])) == b""


# ---------------------------------------------------------------------------
# _require_websockets
# ---------------------------------------------------------------------------


class TestRequireWebsockets:
    def test_succeeds_when_available(self) -> None:
        _require_websockets()

    def test_raises_when_missing(self) -> None:
        import builtins

        real_import = builtins.__import__

        def mock_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "websockets":
                raise ImportError("no websockets")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            try:
                _require_websockets()
                raise AssertionError("should have raised")
            except ImportError as exc:
                assert "websockets is required" in str(exc)


# ---------------------------------------------------------------------------
# _handle_ws_control / _handle_ws_control_frame
# ---------------------------------------------------------------------------


class TestHandleWsControlFrame:
    async def test_session_token_saves(self, tmp_path: Path) -> None:
        tf = tmp_path / "token"
        write_fn = AsyncMock()
        result = await _handle_ws_control_frame(
            {"type": "session_token", "token": "tok123"}, tf, write_fn
        )
        assert result is True
        assert tf.read_text() == "tok123"

    async def test_session_token_no_file(self) -> None:
        write_fn = AsyncMock()
        result = await _handle_ws_control_frame(
            {"type": "session_token", "token": "tok123"}, None, write_fn
        )
        assert result is False

    async def test_resume_ok(self) -> None:
        write_fn = AsyncMock()
        result = await _handle_ws_control_frame({"type": "resume_ok"}, None, write_fn)
        assert result is True
        write_fn.assert_called_once_with(b"\r\n[Session resumed]\r\n")

    async def test_resume_failed_deletes_token(self, tmp_path: Path) -> None:
        tf = tmp_path / "token"
        tf.write_text("old")
        write_fn = AsyncMock()
        result = await _handle_ws_control_frame({"type": "resume_failed"}, tf, write_fn)
        assert result is True
        assert not tf.exists()

    async def test_resume_failed_no_token_file(self) -> None:
        write_fn = AsyncMock()
        result = await _handle_ws_control_frame(
            {"type": "resume_failed"}, None, write_fn
        )
        assert result is True

    async def test_unknown_type(self) -> None:
        write_fn = AsyncMock()
        result = await _handle_ws_control_frame({"type": "unknown"}, None, write_fn)
        assert result is False

    async def test_no_type_key(self) -> None:
        write_fn = AsyncMock()
        result = await _handle_ws_control_frame({"foo": "bar"}, None, write_fn)
        assert result is False

    async def test_non_string_type(self) -> None:
        write_fn = AsyncMock()
        result = await _handle_ws_control_frame({"type": 42}, None, write_fn)
        assert result is False

    async def test_attribute_error_on_data(self) -> None:
        write_fn = AsyncMock()
        result = await _handle_ws_control_frame([1, 2, 3], None, write_fn)  # type: ignore[arg-type]
        assert result is False


class TestHandleWsControl:
    async def test_control_channel_encoded(self, tmp_path: Path) -> None:
        tf = tmp_path / "token"
        write_fn = AsyncMock()
        msg = encode_control({"type": "session_token", "token": "abc"})
        result = await _handle_ws_control(msg, tf, write_fn)
        assert result is True
        assert tf.read_text() == "abc"

    async def test_data_chunk_returns_false(self) -> None:
        write_fn = AsyncMock()
        msg = encode_data("hello")
        result = await _handle_ws_control(msg, None, write_fn)
        assert result is False

    async def test_plain_json_fallback(self, tmp_path: Path) -> None:
        """Trigger the ControlChannelProtocolError -> JSON fallback path."""
        tf = tmp_path / "token"
        write_fn = AsyncMock()
        with patch("provide.terminal.gateway._gateway.ControlChannelDecoder") as mock_cls:
            from provide.terminal.control_channel import ControlChannelProtocolError

            instance = mock_cls.return_value
            instance.feed.side_effect = ControlChannelProtocolError("test")
            msg = json.dumps({"type": "session_token", "token": "xyz"})
            result = await _handle_ws_control(msg, tf, write_fn)
        assert result is True
        assert tf.read_text() == "xyz"

    async def test_plain_json_non_dict_fallback(self) -> None:
        """Fallback path: valid JSON but not a dict."""
        write_fn = AsyncMock()
        with patch("provide.terminal.gateway._gateway.ControlChannelDecoder") as mock_cls:
            from provide.terminal.control_channel import ControlChannelProtocolError

            instance = mock_cls.return_value
            instance.feed.side_effect = ControlChannelProtocolError("test")
            result = await _handle_ws_control(json.dumps([1, 2]), None, write_fn)
        assert result is False

    async def test_invalid_json_fallback_returns_false(self) -> None:
        """Fallback path: not valid JSON either."""
        write_fn = AsyncMock()
        with patch("provide.terminal.gateway._gateway.ControlChannelDecoder") as mock_cls:
            from provide.terminal.control_channel import ControlChannelProtocolError

            instance = mock_cls.return_value
            instance.feed.side_effect = ControlChannelProtocolError("test")
            result = await _handle_ws_control("not json {{{", None, write_fn)
        assert result is False

    async def test_empty_events(self) -> None:
        write_fn = AsyncMock()
        result = await _handle_ws_control("", None, write_fn)
        assert result is False

    async def test_resume_ok_via_control_channel(self) -> None:
        write_fn = AsyncMock()
        msg = encode_control({"type": "resume_ok"})
        result = await _handle_ws_control(msg, None, write_fn)
        assert result is True
        write_fn.assert_called_once()


# ---------------------------------------------------------------------------
# _make_no_auth_server_class
# ---------------------------------------------------------------------------


class TestMakeNoAuthServerClass:
    def test_returns_class(self) -> None:
        import asyncssh

        cls = _make_no_auth_server_class()
        assert issubclass(cls, asyncssh.SSHServer)

    def test_begin_auth_returns_false(self) -> None:
        cls = _make_no_auth_server_class()
        server = cls()
        assert server.begin_auth("anyuser") is False
