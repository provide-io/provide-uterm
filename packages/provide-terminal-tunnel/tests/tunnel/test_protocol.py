#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for tunnel binary frame protocol."""

from __future__ import annotations

import pytest

from provide.terminal.tunnel.protocol import (
    CHANNEL_CONTROL,
    CHANNEL_DATA,
    FLAG_DATA,
    FLAG_EOF,
    TunnelFrame,
    TunnelProtocolError,
    decode_control,
    decode_frame,
    encode_control,
    encode_frame,
)


class TestEncodeFrame:
    def test_data_frame(self) -> None:
        raw = encode_frame(CHANNEL_DATA, b"hello")
        assert raw == bytes([0x01, 0x00]) + b"hello"

    def test_control_frame(self) -> None:
        raw = encode_frame(CHANNEL_CONTROL, b'{"type":"open"}')
        assert raw[0] == 0x00
        assert raw[1] == 0x00
        assert raw[2:] == b'{"type":"open"}'

    def test_eof_flag(self) -> None:
        raw = encode_frame(CHANNEL_DATA, b"", flags=FLAG_EOF)
        assert raw == bytes([0x01, 0x01])

    def test_empty_payload(self) -> None:
        raw = encode_frame(CHANNEL_DATA, b"")
        assert raw == bytes([0x01, 0x00])

    def test_max_channel(self) -> None:
        raw = encode_frame(0xFF, b"x")
        assert raw[0] == 0xFF

    def test_channel_out_of_range(self) -> None:
        with pytest.raises(TunnelProtocolError, match="channel must be"):
            encode_frame(256, b"x")

    def test_negative_channel(self) -> None:
        with pytest.raises(TunnelProtocolError, match="channel must be"):
            encode_frame(-1, b"x")

    def test_flags_out_of_range(self) -> None:
        with pytest.raises(TunnelProtocolError, match="flags must be"):
            encode_frame(0, b"x", flags=256)

    def test_negative_flags(self) -> None:
        with pytest.raises(TunnelProtocolError, match="flags must be"):
            encode_frame(0, b"x", flags=-1)

    def test_large_payload(self) -> None:
        payload = b"\x00" * 65536
        raw = encode_frame(CHANNEL_DATA, payload)
        assert len(raw) == 65536 + 2

    def test_arbitrary_channel(self) -> None:
        raw = encode_frame(0x05, b"stderr")
        assert raw[0] == 0x05


class TestDecodeFrame:
    def test_roundtrip_data(self) -> None:
        raw = encode_frame(CHANNEL_DATA, b"hello world")
        frame = decode_frame(raw)
        assert frame.channel == CHANNEL_DATA
        assert frame.flags == FLAG_DATA
        assert frame.payload == b"hello world"

    def test_roundtrip_eof(self) -> None:
        raw = encode_frame(CHANNEL_DATA, b"", flags=FLAG_EOF)
        frame = decode_frame(raw)
        assert frame.is_eof
        assert frame.payload == b""

    def test_roundtrip_control(self) -> None:
        raw = encode_frame(CHANNEL_CONTROL, b'{"type":"open"}')
        frame = decode_frame(raw)
        assert frame.is_control
        assert frame.payload == b'{"type":"open"}'

    def test_too_short_empty(self) -> None:
        with pytest.raises(TunnelProtocolError, match="frame too short"):
            decode_frame(b"")

    def test_too_short_one_byte(self) -> None:
        with pytest.raises(TunnelProtocolError, match="frame too short"):
            decode_frame(b"\x00")

    def test_minimum_frame(self) -> None:
        frame = decode_frame(bytes([0x01, 0x00]))
        assert frame.channel == CHANNEL_DATA
        assert frame.payload == b""

    def test_binary_payload_preserved(self) -> None:
        payload = bytes(range(256))
        raw = encode_frame(CHANNEL_DATA, payload)
        frame = decode_frame(raw)
        assert frame.payload == payload


class TestTunnelFrame:
    def test_is_eof_false(self) -> None:
        f = TunnelFrame(channel=1, flags=FLAG_DATA, payload=b"x")
        assert not f.is_eof

    def test_is_eof_true(self) -> None:
        f = TunnelFrame(channel=1, flags=FLAG_EOF, payload=b"")
        assert f.is_eof

    def test_is_control_true(self) -> None:
        f = TunnelFrame(channel=CHANNEL_CONTROL, flags=0, payload=b"{}")
        assert f.is_control

    def test_is_control_false(self) -> None:
        f = TunnelFrame(channel=CHANNEL_DATA, flags=0, payload=b"x")
        assert not f.is_control

    def test_frozen(self) -> None:
        f = TunnelFrame(channel=0, flags=0, payload=b"")
        with pytest.raises(AttributeError):
            f.channel = 1  # type: ignore[misc]


class TestEncodeControl:
    def test_basic(self) -> None:
        raw = encode_control({"type": "open", "channel": 1})
        frame = decode_frame(raw)
        assert frame.is_control
        obj = decode_control(frame.payload)
        assert obj["type"] == "open"
        assert obj["channel"] == 1

    def test_missing_type_key(self) -> None:
        with pytest.raises(TunnelProtocolError, match="must have a 'type' key"):
            encode_control({"channel": 1})

    def test_compact_json(self) -> None:
        raw = encode_control({"type": "open", "key": "value"})
        frame = decode_frame(raw)
        text = frame.payload.decode()
        assert " " not in text  # compact separators


class TestDecodeControl:
    def test_valid_json(self) -> None:
        obj = decode_control(b'{"type":"open","channel":1}')
        assert obj == {"type": "open", "channel": 1}

    def test_invalid_json(self) -> None:
        with pytest.raises(TunnelProtocolError, match="invalid control payload"):
            decode_control(b"not json")

    def test_non_dict(self) -> None:
        with pytest.raises(TunnelProtocolError, match="must be a JSON object"):
            decode_control(b"[1,2,3]")

    def test_invalid_utf8(self) -> None:
        with pytest.raises(TunnelProtocolError, match="invalid control payload"):
            decode_control(b"\xff\xfe")

    def test_nested_objects(self) -> None:
        payload = b'{"type":"open","config":{"term_size":[80,24]}}'
        obj = decode_control(payload)
        assert obj["config"]["term_size"] == [80, 24]
