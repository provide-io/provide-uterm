#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Coverage tests for gateway/_gateway.py uncovered branches.

Targets:
- lines 86-93: ControlFrameProtocolError fallback to JSON parse in _handle_ws_control
- line 96: empty events list → return False
- lines 112-113: AttributeError in _handle_ws_control_frame → return False
- lines 236-237: ControlFrameProtocolError in _ws_to_tcp → continue
- lines 318-319: ControlFrameProtocolError in _ws_to_ssh → continue
"""

from __future__ import annotations

import asyncio
from typing import cast

from provide.uterm.control_channel import encode_terminal_data
from provide.uterm.gateway._gateway import (
    _handle_ws_control,
    _handle_ws_control_frame,
    _ws_to_ssh,
    _ws_to_tcp,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _async_iter(items):
    """Return an async iterator over *items*."""

    async def _gen():
        for item in items:
            yield item

    return _gen()


class _MockWriter:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass


# ---------------------------------------------------------------------------
# _handle_ws_control — lines 86-93: ControlFrameProtocolError → JSON library fallback
# ---------------------------------------------------------------------------


class TestHandleWsControlProtocolErrorFallback:
    """ControlFrameProtocolError is raised by decoder.feed → fall back to JSON parse.

    The control channel decoder raises ControlFrameProtocolError when it encounters
    the DLE character (\\x10) followed by an invalid byte (not DLE or STX).
    We prefix messages with DLE+X to reliably trigger this error path.
    """

    async def test_invalid_control_channel_valid_json_dict_with_type(self) -> None:
        """Lines 87-93: decode fails → JSON parse succeeds → dict with type → dispatched.

        Prefix with DLE+X to force ControlFrameProtocolError, then the message
        body is valid JSON with a known type → _handle_ws_control_frame is called.
        """
        written: list[bytes] = []

        async def _write_fn(data: bytes) -> None:
            written.append(data)

        # DLE (\x10) + 'X' triggers 'invalid control prefix' ProtocolError.
        # The full message IS valid JSON after stripping the prefix — but since
        # the entire message string is passed to json.loads, we need it to be
        # valid JSON by itself. We use a JSON-valid string that starts with DLE+X.
        # Actually: the entire `message` arg is passed to json.loads on fallback.
        # So we need the message to be valid JSON AND start with a DLE+X sequence.
        # That's impossible since DLE is not valid JSON.
        # Instead: patch decoder.feed to raise directly, then use a valid JSON message body.
        from unittest.mock import patch

        from provide.uterm.control_channel import (
            ControlFrameDecoder,
            ControlFrameProtocolError,
        )

        def _raise_protocol_error(self, data):
            raise ControlFrameProtocolError("injected")

        with patch.object(ControlFrameDecoder, "feed", _raise_protocol_error):
            msg = '{"type": "resume_ok"}'
            result = await _handle_ws_control(msg, [None], _write_fn)

        # resume_ok should be handled → True, and writes a message
        assert result is True
        assert any(b"resumed" in w.lower() for w in written)

    async def test_invalid_control_channel_invalid_json_returns_false(self) -> None:
        """Lines 89-90: decode fails AND JSON parse fails → return False."""
        written: list[bytes] = []

        async def _write_fn(data: bytes) -> None:
            written.append(data)

        from unittest.mock import patch

        from provide.uterm.control_channel import (
            ControlFrameDecoder,
            ControlFrameProtocolError,
        )

        def _raise_protocol_error(self, data):
            raise ControlFrameProtocolError("injected")

        with patch.object(ControlFrameDecoder, "feed", _raise_protocol_error):
            msg = "not-valid-json-at-all-{{{"
            result = await _handle_ws_control(msg, [None], _write_fn)

        assert result is False
        assert written == []

    async def test_invalid_control_channel_json_non_dict_returns_false(self) -> None:
        """Lines 91-92: decode fails, JSON parse succeeds but result is not a dict → return False."""
        written: list[bytes] = []

        async def _write_fn(data: bytes) -> None:
            written.append(data)

        from unittest.mock import patch

        from provide.uterm.control_channel import (
            ControlFrameDecoder,
            ControlFrameProtocolError,
        )

        def _raise_protocol_error(self, data):
            raise ControlFrameProtocolError("injected")

        with patch.object(ControlFrameDecoder, "feed", _raise_protocol_error):
            # Valid JSON list — not a dict
            msg = "[1, 2, 3]"
            result = await _handle_ws_control(msg, [None], _write_fn)

        assert result is False
        assert written == []

    async def test_invalid_control_channel_json_string_returns_false(self) -> None:
        """Lines 91-92: JSON parse succeeds but result is a string, not dict → return False."""
        written: list[bytes] = []

        async def _write_fn(data: bytes) -> None:
            written.append(data)

        from unittest.mock import patch

        from provide.uterm.control_channel import (
            ControlFrameDecoder,
            ControlFrameProtocolError,
        )

        def _raise_protocol_error(self, data):
            raise ControlFrameProtocolError("injected")

        with patch.object(ControlFrameDecoder, "feed", _raise_protocol_error):
            msg = '"just a string"'
            result = await _handle_ws_control(msg, [None], _write_fn)

        assert result is False


# ---------------------------------------------------------------------------
# _handle_ws_control — line 96: empty events → return False
# ---------------------------------------------------------------------------


class TestHandleWsControlEmptyEvents:
    """Line 96: decoder.feed returns empty events → return False (no-op)."""

    async def test_empty_string_returns_false(self) -> None:
        """Empty message → ControlFrameDecoder returns no events → return False."""
        written: list[bytes] = []

        async def _write_fn(data: bytes) -> None:
            written.append(data)

        # Empty string: no events produced by decoder (finish also empty)
        result = await _handle_ws_control("", [None], _write_fn)
        assert result is False
        assert written == []


# ---------------------------------------------------------------------------
# _handle_ws_control_frame — lines 112-113: AttributeError → return False
# ---------------------------------------------------------------------------


class TestHandleWsControlFrameAttributeError:
    """Lines 112-113: data.get("type") raises AttributeError → return False."""

    async def test_non_dict_raises_attribute_error_returns_false(self) -> None:
        """Pass a non-dict (e.g., a list) that has no .get() method → AttributeError → False."""
        written: list[bytes] = []

        async def _write_fn(data: bytes) -> None:
            written.append(data)

        # Lists don't have .get() → AttributeError on data.get("type")
        result = await _handle_ws_control_frame([], None, _write_fn)  # type: ignore[arg-type]
        assert result is False
        assert written == []


# ---------------------------------------------------------------------------
# _ws_to_tcp — lines 236-237: ControlFrameProtocolError → continue (skip message)
# ---------------------------------------------------------------------------


class TestWsToTcpProtocolError:
    """Lines 236-237: ControlFrameProtocolError from decoder.feed → continue to next message."""

    async def test_protocol_error_message_skipped_next_forwarded(self) -> None:
        """A message that corrupts the control channel decoder is skipped; subsequent data is forwarded."""
        writer = _MockWriter()

        # First send a valid data message to forward; the decoder will process it.
        # To trigger ControlFrameProtocolError we need to corrupt decoder state.
        # We patch the decoder's feed method to raise on first call only.
        from unittest.mock import patch

        from provide.uterm.control_channel import (
            ControlFrameDecoder,
            ControlFrameProtocolError,
        )

        call_count = 0
        original_feed = ControlFrameDecoder.feed

        def patched_feed(self, data):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ControlFrameProtocolError("bad stream")
            return original_feed(self, data)

        # Valid data message that should be forwarded after the error is skipped
        valid_msg = encode_terminal_data("hello")

        with patch.object(ControlFrameDecoder, "feed", patched_feed):
            await _ws_to_tcp(
                _async_iter(["corrupted", valid_msg]),
                cast("asyncio.StreamWriter", writer),
                token_holder=[None],
            )

        # The first message triggered ControlFrameProtocolError (continue),
        # the second was forwarded.
        assert any(b"hello" in w for w in writer.written)

    async def test_binary_message_forwarded_directly(self) -> None:
        """Lines 249-254: binary (bytes) message bypasses the decoder and is written directly."""
        writer = _MockWriter()

        raw_bytes = b"\x1b[32mgreen\x1b[0m"
        await _ws_to_tcp(
            _async_iter([raw_bytes]),
            cast("asyncio.StreamWriter", writer),
            token_holder=[None],
        )
        # Binary data is written as-is (with DEL→BS and CRLF normalization applied)
        combined = b"".join(writer.written)
        assert b"green" in combined


# ---------------------------------------------------------------------------
# _ws_to_ssh — lines 318-319: ControlFrameProtocolError → continue
# ---------------------------------------------------------------------------


class TestWsToSshProtocolError:
    """Lines 318-319: ControlFrameProtocolError from decoder.feed in _ws_to_ssh → continue."""

    async def test_protocol_error_skipped_next_forwarded(self) -> None:
        """A corrupted control channel message is skipped; next data is forwarded to SSH stdout."""
        stdout_writes: list[str] = []

        class _MockProcess:
            class Stdout:
                @staticmethod
                def write(data) -> None:
                    stdout_writes.append(data if isinstance(data, str) else data.decode())

            stdout = Stdout

        from unittest.mock import patch

        from provide.uterm.control_channel import (
            ControlFrameDecoder,
            ControlFrameProtocolError,
        )

        call_count = 0
        original_feed = ControlFrameDecoder.feed

        def patched_feed(self, data):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ControlFrameProtocolError("bad")
            return original_feed(self, data)

        valid_msg = encode_terminal_data("world")

        with patch.object(ControlFrameDecoder, "feed", patched_feed):
            await _ws_to_ssh(
                _async_iter(["corrupted", valid_msg]),
                _MockProcess(),
                token_holder=[None],
            )

        combined = "".join(stdout_writes)
        assert "world" in combined

    async def test_binary_message_forwarded_to_ssh(self) -> None:
        """Lines 327-329: binary message in _ws_to_ssh goes to else branch, written to stdout."""
        stdout_writes: list[str] = []

        class _MockProcess:
            class Stdout:
                @staticmethod
                def write(data) -> None:
                    stdout_writes.append(data if isinstance(data, str) else data.decode())

            stdout = Stdout

        raw_bytes = b"ssh output"
        await _ws_to_ssh(
            _async_iter([raw_bytes]),
            _MockProcess(),
            token_holder=[None],
        )

        combined = "".join(stdout_writes)
        assert "ssh output" in combined
