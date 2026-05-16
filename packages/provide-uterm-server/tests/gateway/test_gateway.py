#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.gateway._gateway — non-pump helper functions."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

from provide.uterm.control_channel import encode_control, encode_data
from provide.uterm.gateway._gateway import (
    _handle_ws_control,
    _handle_ws_control_frame,
    _normalize_crlf,
    _require_websockets,
    _skip_subneg_sequence,
    _strip_iac,
)
from provide.uterm.gateway._ssh_handler import _make_no_auth_server_class

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
    async def test_session_token_updates_holder(self) -> None:
        holder: list[dict | None] = [None]
        write_fn = AsyncMock()
        result = await _handle_ws_control_frame({"type": "session_token", "token": "tok123"}, holder, write_fn)
        assert result is True
        assert holder[0] == {"token": "tok123"}

    async def test_session_token_with_player_id(self) -> None:
        holder: list[dict | None] = [None]
        write_fn = AsyncMock()
        result = await _handle_ws_control_frame(
            {"type": "session_token", "token": "tok123", "player_id": 7}, holder, write_fn
        )
        assert result is True
        assert holder[0] == {"token": "tok123", "player_id": 7}

    async def test_resume_ok(self) -> None:
        holder: list[dict | None] = [None]
        write_fn = AsyncMock()
        result = await _handle_ws_control_frame({"type": "resume_ok"}, holder, write_fn)
        assert result is True
        write_fn.assert_called_once_with(b"\r\n[Session resumed]\r\n")

    async def test_resume_failed_clears_holder(self) -> None:
        holder: list[dict | None] = [{"token": "old"}]
        write_fn = AsyncMock()
        result = await _handle_ws_control_frame({"type": "resume_failed"}, holder, write_fn)
        assert result is True
        assert holder[0] is None

    async def test_unknown_type(self) -> None:
        holder: list[dict | None] = [None]
        write_fn = AsyncMock()
        result = await _handle_ws_control_frame({"type": "unknown"}, holder, write_fn)
        assert result is False

    async def test_no_type_key(self) -> None:
        holder: list[dict | None] = [None]
        write_fn = AsyncMock()
        result = await _handle_ws_control_frame({"foo": "bar"}, holder, write_fn)
        assert result is False

    async def test_non_string_type(self) -> None:
        holder: list[dict | None] = [None]
        write_fn = AsyncMock()
        result = await _handle_ws_control_frame({"type": 42}, holder, write_fn)
        assert result is False

    async def test_attribute_error_on_data(self) -> None:
        holder: list[dict | None] = [None]
        write_fn = AsyncMock()
        result = await _handle_ws_control_frame([1, 2, 3], holder, write_fn)  # type: ignore[arg-type]
        assert result is False


class TestHandleWsControl:
    async def test_control_channel_encoded(self) -> None:
        holder: list[dict | None] = [None]
        write_fn = AsyncMock()
        msg = encode_control({"type": "session_token", "token": "abc"})
        result = await _handle_ws_control(msg, holder, write_fn)
        assert result is True
        assert holder[0] == {"token": "abc"}

    async def test_data_chunk_returns_false(self) -> None:
        holder: list[dict | None] = [None]
        write_fn = AsyncMock()
        msg = encode_data("hello")
        result = await _handle_ws_control(msg, holder, write_fn)
        assert result is False

    async def test_plain_json_fallback(self) -> None:
        """Trigger the ControlChannelProtocolError -> JSON fallback path."""
        holder: list[dict | None] = [None]
        write_fn = AsyncMock()
        with patch("provide.uterm.gateway._gateway.ControlChannelDecoder") as mock_cls:
            from provide.uterm.control_channel import ControlChannelProtocolError

            instance = mock_cls.return_value
            instance.feed.side_effect = ControlChannelProtocolError("test")
            msg = json.dumps({"type": "session_token", "token": "xyz"})
            result = await _handle_ws_control(msg, holder, write_fn)
        assert result is True
        assert holder[0] == {"token": "xyz"}

    async def test_plain_json_non_dict_fallback(self) -> None:
        """Fallback path: valid JSON but not a dict."""
        holder: list[dict | None] = [None]
        write_fn = AsyncMock()
        with patch("provide.uterm.gateway._gateway.ControlChannelDecoder") as mock_cls:
            from provide.uterm.control_channel import ControlChannelProtocolError

            instance = mock_cls.return_value
            instance.feed.side_effect = ControlChannelProtocolError("test")
            result = await _handle_ws_control(json.dumps([1, 2]), holder, write_fn)
        assert result is False

    async def test_invalid_json_fallback_returns_false(self) -> None:
        """Fallback path: not valid JSON either."""
        holder: list[dict | None] = [None]
        write_fn = AsyncMock()
        with patch("provide.uterm.gateway._gateway.ControlChannelDecoder") as mock_cls:
            from provide.uterm.control_channel import ControlChannelProtocolError

            instance = mock_cls.return_value
            instance.feed.side_effect = ControlChannelProtocolError("test")
            result = await _handle_ws_control("not json {{{", holder, write_fn)
        assert result is False

    async def test_empty_events(self) -> None:
        holder: list[dict | None] = [None]
        write_fn = AsyncMock()
        result = await _handle_ws_control("", holder, write_fn)
        assert result is False

    async def test_resume_ok_via_control_channel(self) -> None:
        holder: list[dict | None] = [None]
        write_fn = AsyncMock()
        msg = encode_control({"type": "resume_ok"})
        result = await _handle_ws_control(msg, holder, write_fn)
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

    async def test_begin_auth_requires_auth_but_accepts_all(self) -> None:
        """begin_auth returns True so asyncssh exercises the pubkey handler,
        but validate_public_key / validate_password accept unconditionally so
        the gate behaves no-auth to callers while making the offered pubkey
        available for per-fingerprint token routing."""
        cls = _make_no_auth_server_class()
        server = cls()
        assert server.begin_auth("anyuser") is True
        assert await server.validate_public_key("anyuser", object()) is True
        assert server.validate_password("anyuser", "anything") is True
        assert server.public_key_auth_supported() is True
        assert server.password_auth_supported() is True
