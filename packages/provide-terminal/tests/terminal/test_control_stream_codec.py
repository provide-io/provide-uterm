#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the inline control stream codec."""

from __future__ import annotations

import pytest

from provide.terminal.control_stream import (
    DLE,
    STX,
    ControlChunk,
    ControlStreamDecoder,
    ControlStreamProtocolError,
    DataChunk,
    encode_control,
    encode_data,
)


def test_encode_data_escapes_dle() -> None:
    assert encode_data(f"a{DLE}b") == f"a{DLE}{DLE}b"


def test_encode_control_builds_prefixed_ascii_frame() -> None:
    encoded = encode_control({"type": "hello", "ok": True})
    assert encoded.startswith(f"{DLE}{STX}")
    assert encoded[10] == ":"
    assert '"type":"hello"' in encoded


def test_decoder_returns_raw_passthrough_data() -> None:
    decoder = ControlStreamDecoder()
    assert decoder.feed("hello world") == [DataChunk("hello world")]


def test_decoder_returns_control_frame() -> None:
    decoder = ControlStreamDecoder()
    decoded = decoder.feed(encode_control({"type": "snapshot_req"}))
    assert decoded == [ControlChunk({"type": "snapshot_req"})]


def test_decoder_handles_back_to_back_frames() -> None:
    decoder = ControlStreamDecoder()
    raw = encode_control({"type": "one"}) + encode_control({"type": "two"})
    assert decoder.feed(raw) == [ControlChunk({"type": "one"}), ControlChunk({"type": "two"})]


def test_decoder_handles_mixed_data_and_control() -> None:
    decoder = ControlStreamDecoder()
    raw = encode_data("before") + encode_control({"type": "ping"}) + encode_data("after")
    assert decoder.feed(raw) == [DataChunk("before"), ControlChunk({"type": "ping"}), DataChunk("after")]


def test_decoder_handles_split_control_frame() -> None:
    decoder = ControlStreamDecoder()
    encoded = encode_control({"type": "resume", "token": "abc"})
    midpoint = len(encoded) // 2
    assert decoder.feed(encoded[:midpoint]) == []
    assert decoder.feed(encoded[midpoint:]) == [ControlChunk({"type": "resume", "token": "abc"})]


def test_decoder_handles_escaped_literal_dle() -> None:
    decoder = ControlStreamDecoder()
    assert decoder.feed(encode_data(f"x{DLE}y")) == [DataChunk(f"x{DLE}y")]


def test_decoder_rejects_invalid_prefix() -> None:
    decoder = ControlStreamDecoder()
    with pytest.raises(ControlStreamProtocolError, match="invalid control prefix"):
        decoder.feed(f"{DLE}x")


def test_decoder_rejects_bad_length_header() -> None:
    decoder = ControlStreamDecoder()
    with pytest.raises(ControlStreamProtocolError, match="invalid control header"):
        decoder.feed(f"{DLE}{STX}zzzzzzzz:{{}}")


def test_decoder_rejects_invalid_json_payload() -> None:
    decoder = ControlStreamDecoder()
    raw = f"{DLE}{STX}00000002:[]"
    with pytest.raises(ControlStreamProtocolError, match="control payload must be an object"):
        decoder.feed(raw)


def test_decoder_rejects_payload_over_limit() -> None:
    decoder = ControlStreamDecoder(max_control_payload_bytes=5)
    with pytest.raises(ControlStreamProtocolError, match="control payload too large"):
        decoder.feed(encode_control({"type": "much-too-large"}))


def test_finish_rejects_truncated_payload() -> None:
    decoder = ControlStreamDecoder()
    encoded = encode_control({"type": "hello"})
    decoder.feed(encoded[:-1])
    with pytest.raises(ControlStreamProtocolError, match="truncated control frame"):
        decoder.finish()
