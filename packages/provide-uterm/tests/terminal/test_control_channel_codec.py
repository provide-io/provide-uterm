#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the inline control channel codec."""

from __future__ import annotations

import pytest

from provide.uterm.control_channel import (
    DLE,
    STX,
    ControlChannelDecoder,
    ControlChannelProtocolError,
    ControlChunk,
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


def test_encode_control_lengths_raw_unicode_payload_in_utf8_bytes() -> None:
    encoded = encode_control({"type": "hello", "text": "👋"})
    payload = encoded[11:]
    assert "👋" in payload
    assert int(encoded[2:10], 16) == len(payload.encode("utf-8"))


def test_decoder_returns_raw_passthrough_data() -> None:
    decoder = ControlChannelDecoder()
    assert decoder.feed("hello world") == [DataChunk("hello world")]


def test_decoder_returns_control_frame() -> None:
    decoder = ControlChannelDecoder()
    decoded = decoder.feed(encode_control({"type": "snapshot_req"}))
    assert decoded == [ControlChunk({"type": "snapshot_req"})]


def test_decoder_reads_utf8_byte_length_for_non_bmp_payload() -> None:
    decoder = ControlChannelDecoder()
    payload = '{"type":"hello","text":"👋"}'
    raw = f"{DLE}{STX}{len(payload.encode('utf-8')):08x}:{payload}"
    assert decoder.feed(raw) == [ControlChunk({"type": "hello", "text": "👋"})]


def test_decoder_handles_back_to_back_frames() -> None:
    decoder = ControlChannelDecoder()
    raw = encode_control({"type": "one"}) + encode_control({"type": "two"})
    assert decoder.feed(raw) == [ControlChunk({"type": "one"}), ControlChunk({"type": "two"})]


def test_decoder_handles_mixed_data_and_control() -> None:
    decoder = ControlChannelDecoder()
    raw = encode_data("before") + encode_control({"type": "ping"}) + encode_data("after")
    assert decoder.feed(raw) == [DataChunk("before"), ControlChunk({"type": "ping"}), DataChunk("after")]


def test_decoder_handles_split_control_frame() -> None:
    decoder = ControlChannelDecoder()
    encoded = encode_control({"type": "resume", "token": "abc"})
    midpoint = len(encoded) // 2
    assert decoder.feed(encoded[:midpoint]) == []
    assert decoder.feed(encoded[midpoint:]) == [ControlChunk({"type": "resume", "token": "abc"})]


def test_decoder_handles_escaped_literal_dle() -> None:
    decoder = ControlChannelDecoder()
    assert decoder.feed(encode_data(f"x{DLE}y")) == [DataChunk(f"x{DLE}y")]


def test_decoder_rejects_invalid_prefix() -> None:
    decoder = ControlChannelDecoder()
    with pytest.raises(ControlChannelProtocolError, match="invalid control prefix"):
        decoder.feed(f"{DLE}x")


def test_decoder_rejects_bad_length_header() -> None:
    decoder = ControlChannelDecoder()
    with pytest.raises(ControlChannelProtocolError, match="invalid control header"):
        decoder.feed(f"{DLE}{STX}zzzzzzzz:{{}}")


def test_decoder_rejects_invalid_json_payload() -> None:
    decoder = ControlChannelDecoder()
    raw = f"{DLE}{STX}00000002:[]"
    with pytest.raises(ControlChannelProtocolError, match="control payload must be an object"):
        decoder.feed(raw)


def test_decoder_rejects_payload_over_limit() -> None:
    decoder = ControlChannelDecoder(max_control_payload_bytes=5)
    with pytest.raises(ControlChannelProtocolError, match="control payload too large"):
        decoder.feed(encode_control({"type": "much-too-large"}))


def test_finish_rejects_truncated_payload() -> None:
    decoder = ControlChannelDecoder()
    encoded = encode_control({"type": "hello"})
    decoder.feed(encoded[:-1])
    with pytest.raises(ControlChannelProtocolError, match="truncated control frame"):
        decoder.finish()


def test_decoder_rejects_deeply_nested_payload() -> None:
    """Payloads nested deeper than ``max_frame_depth`` are rejected before
    callers can walk them. Default depth is 32; we test with a tight
    custom limit so the test stays cheap."""
    decoder = ControlChannelDecoder(max_frame_depth=4)
    # Build {"type": {"a": {"a": {"a": {"a": "bomb"}}}}} — depth 5.
    payload: dict[str, object] = {"a": "bomb"}
    for _ in range(4):
        payload = {"a": payload}
    payload = {"type": payload}
    with pytest.raises(ControlChannelProtocolError, match="nests deeper than 4"):
        decoder.feed(encode_control(payload))


def test_decoder_rejects_deeply_nested_list_payload() -> None:
    """List nesting counts too: ``{"type": [[[[[...]]]]]}`` is also rejected."""
    decoder = ControlChannelDecoder(max_frame_depth=3)
    nested: object = "leaf"
    for _ in range(5):
        nested = [nested]
    with pytest.raises(ControlChannelProtocolError, match="nests deeper than 3"):
        decoder.feed(encode_control({"type": nested}))


def test_decoder_accepts_payloads_at_depth_limit() -> None:
    decoder = ControlChannelDecoder(max_frame_depth=3)
    # Depth 3: top-level dict (1) → "annotations" dict (2) → "items" list (3).
    payload = {"type": "hello", "annotations": {"items": ["a", "b"]}}
    events = decoder.feed(encode_control(payload))
    assert len(events) == 1


def test_decoder_default_depth_allows_typical_payloads() -> None:
    decoder = ControlChannelDecoder()
    # Flat hello frame should pass with plenty of headroom.
    events = decoder.feed(encode_control({"type": "worker_hello", "worker_id": "w1", "role": "operator"}))
    assert len(events) == 1


def test_decoder_list_depth_increment_is_one_not_two() -> None:
    """Kills mutmut: ``stack.append((child, depth + 1))`` → ``depth + 2``
    in the list branch of ``_check_json_depth``. A depth-4 list inside
    ``max_frame_depth=5`` must parse cleanly under the correct
    increment (depths recorded: 2,3,4,5 → max 5, no trip); under the
    +2 mutant the same input records depths 3,5,7,9 and falsely trips
    at 7 > 5.
    """
    decoder = ControlChannelDecoder(max_frame_depth=5)
    nested: object = "leaf"
    for _ in range(4):
        nested = [nested]
    payload = {"type": nested}
    events = decoder.feed(encode_control(payload))
    assert len(events) == 1


def test_decoder_depth_limit_clamps_to_minimum() -> None:
    """max_frame_depth=0 (or negative) is clamped to 1 — frames are always
    at least one dict deep."""
    decoder = ControlChannelDecoder(max_frame_depth=0)
    # A flat dict is depth 1 — must still parse.
    events = decoder.feed(encode_control({"type": "hello"}))
    assert len(events) == 1
    # But depth-2 nesting should now be rejected.
    decoder2 = ControlChannelDecoder(max_frame_depth=0)
    with pytest.raises(ControlChannelProtocolError, match="nests deeper than 1"):
        decoder2.feed(encode_control({"type": {"nested": "value"}}))
