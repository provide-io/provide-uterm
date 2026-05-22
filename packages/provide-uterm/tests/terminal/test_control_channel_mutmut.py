#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Surgical mutmut-killer tests for ``ControlChannelDecoder``.

Targets the high-density mutation areas: ``__init__`` default values,
the ``feed``/``finish``/``_drain``/``_try_parse_frame`` decoder hot path,
and ``_report_error`` callback invocation.
"""

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
)

# ---------------------------------------------------------------------------
# __init__ defaults
# ---------------------------------------------------------------------------


class TestDecoderInit:
    def test_max_control_payload_bytes_default_is_one_meg(self) -> None:
        d = ControlChannelDecoder()
        assert d._max_control_payload_bytes == 1_048_576

    def test_max_buffer_bytes_default_is_ten_meg(self) -> None:
        d = ControlChannelDecoder()
        assert d._max_buffer_bytes == 10_485_760

    def test_max_control_payload_explicit_overrides_default(self) -> None:
        d = ControlChannelDecoder(max_control_payload_bytes=512)
        assert d._max_control_payload_bytes == 512

    def test_max_buffer_explicit_overrides_default(self) -> None:
        d = ControlChannelDecoder(max_buffer_bytes=2048)
        assert d._max_buffer_bytes == 2048

    def test_max_control_payload_min_clamp(self) -> None:
        """Values <= 0 clamp up to 1."""
        d = ControlChannelDecoder(max_control_payload_bytes=0)
        assert d._max_control_payload_bytes == 1

    def test_max_buffer_min_clamp(self) -> None:
        d = ControlChannelDecoder(max_buffer_bytes=-100)
        assert d._max_buffer_bytes == 1

    def test_buffer_starts_empty_string(self) -> None:
        d = ControlChannelDecoder()
        assert d._buffer == ""

    def test_buffer_parts_starts_empty_list(self) -> None:
        d = ControlChannelDecoder()
        assert d._buffer_parts == []

    def test_on_error_defaults_to_none(self) -> None:
        d = ControlChannelDecoder()
        assert d._on_error is None

    def test_on_error_stored_as_attribute(self) -> None:
        def cb(_: str) -> None:
            return None

        d = ControlChannelDecoder(on_error=cb)
        assert d._on_error is cb


# ---------------------------------------------------------------------------
# feed: type checking + buffer overflow + drain integration
# ---------------------------------------------------------------------------


class TestFeed:
    def test_non_string_chunk_raises_typeerror_with_actual_type(self) -> None:
        d = ControlChannelDecoder()
        with pytest.raises(TypeError) as exc_info:
            d.feed(b"bytes")  # type: ignore[arg-type]
        # Mutation that replaces ``type(chunk).__name__`` with
        # ``type(None).__name__`` (== 'NoneType') would put 'NoneType' in the
        # message instead of 'bytes'.
        assert "bytes" in str(exc_info.value), f"expected 'bytes' in {exc_info.value!r}"

    def test_non_string_chunk_type_is_not_nonetype_in_error(self) -> None:
        d = ControlChannelDecoder()
        with pytest.raises(TypeError) as exc_info:
            d.feed(42)  # type: ignore[arg-type]
        # If the mutation forces type(None) we'd see 'NoneType'; assert we don't.
        assert "NoneType" not in str(exc_info.value)
        assert "int" in str(exc_info.value)

    def test_buffer_overflow_raises_protocol_error(self) -> None:
        d = ControlChannelDecoder(max_buffer_bytes=10)
        with pytest.raises(ControlChannelProtocolError) as exc_info:
            d.feed("x" * 11)
        assert "overflow" in str(exc_info.value)

    def test_buffer_overflow_resets_state(self) -> None:
        d = ControlChannelDecoder(max_buffer_bytes=5)
        try:
            d.feed("toolong")
        except ControlChannelProtocolError:
            pass
        # State must be cleared after overflow.
        assert d._buffer == ""
        assert d._buffer_parts == []

    def test_feed_returns_pass_through_data_chunk(self) -> None:
        d = ControlChannelDecoder()
        events = d.feed("hello")
        assert len(events) == 1
        assert isinstance(events[0], DataChunk)
        assert events[0].data == "hello"

    def test_feed_decodes_complete_control_frame(self) -> None:
        d = ControlChannelDecoder()
        frame = encode_control({"type": "test", "x": 1})
        events = d.feed(frame)
        controls = [e for e in events if isinstance(e, ControlChunk)]
        assert len(controls) == 1
        assert controls[0].control == {"type": "test", "x": 1}

    def test_feed_buffers_incomplete_control_frame(self) -> None:
        d = ControlChannelDecoder()
        frame = encode_control({"type": "split"})
        events1 = d.feed(frame[:5])
        events2 = d.feed(frame[5:])
        # The combined feed should yield exactly one control event.
        all_controls = [e for e in events1 + events2 if isinstance(e, ControlChunk)]
        assert len(all_controls) == 1
        assert all_controls[0].control == {"type": "split"}

    def test_feed_buffer_parts_collapsed_after_drain(self) -> None:
        d = ControlChannelDecoder()
        d.feed("complete data\n")
        # After a successful feed with no unconsumed data, _buffer_parts is empty.
        assert d._buffer_parts == []

    def test_feed_protocol_error_clears_state_exactly(self) -> None:
        d = ControlChannelDecoder()
        with pytest.raises(ControlChannelProtocolError) as exc_info:
            d.feed(f"{DLE}x")
        assert str(exc_info.value) == "invalid control prefix"
        assert d._buffer == ""
        assert d._buffer_parts == []


# ---------------------------------------------------------------------------
# finish: truncated frame detection + final-flag propagation
# ---------------------------------------------------------------------------


class TestFinish:
    def test_finish_on_empty_buffer_returns_empty(self) -> None:
        d = ControlChannelDecoder()
        assert d.finish() == []

    def test_finish_with_truncated_control_frame_raises(self) -> None:
        """A control-frame prefix without complete header is truncated."""
        d = ControlChannelDecoder()
        # Feed only the DLE STX prefix (2 chars). Not enough for header bytes.
        d.feed("\x10\x02")
        with pytest.raises(ControlChannelProtocolError) as exc:
            d.finish()
        assert str(exc.value) == "truncated control frame"

    def test_finish_resets_state_on_protocol_error(self) -> None:
        d = ControlChannelDecoder()
        d.feed("\x10\x02")
        try:
            d.finish()
        except ControlChannelProtocolError:
            pass
        assert d._buffer == ""
        assert d._buffer_parts == []

    def test_finish_drains_remaining_data_chunk(self) -> None:
        d = ControlChannelDecoder()
        # Plain data without a DLE STX prefix — yielded as DataChunk.
        events = d.feed("trailing")
        # finish() with empty buffer must succeed.
        assert d.finish() == []
        assert any(isinstance(e, DataChunk) for e in events)

    def test_finish_residual_buffer_clears_state_exactly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        d = ControlChannelDecoder()
        d._buffer = "leftover"
        d._buffer_parts = ["leftover"]
        monkeypatch.setattr(d, "_drain", lambda *, final: [])

        with pytest.raises(ControlChannelProtocolError) as exc_info:
            d.finish()

        assert str(exc_info.value) == "truncated control frame"
        assert d._buffer == ""
        assert d._buffer_parts == []


class TestProtocolErrorMessages:
    def test_invalid_json_message_is_exact(self) -> None:
        d = ControlChannelDecoder()
        raw = f"{DLE}{STX}00000008:not-json"
        with pytest.raises(ControlChannelProtocolError) as exc_info:
            d.feed(raw)
        assert str(exc_info.value) == "invalid control json"

    def test_non_object_payload_message_is_exact(self) -> None:
        d = ControlChannelDecoder()
        raw = f"{DLE}{STX}00000002:[]"
        with pytest.raises(ControlChannelProtocolError) as exc_info:
            d.feed(raw)
        assert str(exc_info.value) == "control payload must be an object"

    def test_invalid_utf8_length_message_is_exact(self) -> None:
        d = ControlChannelDecoder()
        payload = '{"emoji":"😀"}'
        declared_bytes = payload.encode("utf-8").index("😀".encode()) + 1
        raw = f"{DLE}{STX}{declared_bytes:08x}:{payload}"
        with pytest.raises(ControlChannelProtocolError) as exc_info:
            d.feed(raw)
        assert str(exc_info.value) == "invalid control payload length"

    def test_incomplete_header_message_is_exact(self) -> None:
        d = ControlChannelDecoder()
        d.feed(f"{DLE}{STX}0000")
        with pytest.raises(ControlChannelProtocolError) as exc_info:
            d.finish()
        assert str(exc_info.value) == "truncated control frame"

    def test_invalid_header_message_is_exact(self) -> None:
        d = ControlChannelDecoder()
        with pytest.raises(ControlChannelProtocolError) as exc_info:
            d.feed(f"{DLE}{STX}zzzzzzzz:{{}}")
        assert str(exc_info.value) == "invalid control header"

    def test_payload_too_large_message_is_exact(self) -> None:
        d = ControlChannelDecoder(max_control_payload_bytes=5)
        with pytest.raises(ControlChannelProtocolError) as exc_info:
            d.feed(encode_control({"k": "v"}))
        assert str(exc_info.value) == "control payload too large"

    def test_incomplete_payload_message_is_exact(self) -> None:
        d = ControlChannelDecoder()
        d.feed(f"{DLE}{STX}00000008:{{")
        with pytest.raises(ControlChannelProtocolError) as exc_info:
            d.finish()
        assert str(exc_info.value) == "truncated control frame"

    def test_trailing_dle_message_is_exact(self) -> None:
        d = ControlChannelDecoder()
        d.feed(DLE)
        with pytest.raises(ControlChannelProtocolError) as exc_info:
            d.finish()
        assert str(exc_info.value) == "truncated control frame"

    def test_invalid_prefix_message_is_exact(self) -> None:
        d = ControlChannelDecoder()
        with pytest.raises(ControlChannelProtocolError) as exc_info:
            d.feed(f"{DLE}x")
        assert str(exc_info.value) == "invalid control prefix"

    def test_empty_control_payload_uses_invalid_json_error(self) -> None:
        d = ControlChannelDecoder()
        with pytest.raises(ControlChannelProtocolError) as exc_info:
            d.feed(f"{DLE}{STX}00000000:")
        assert str(exc_info.value) == "invalid control json"


# ---------------------------------------------------------------------------
# _report_error: on_error callback invocation
# ---------------------------------------------------------------------------


class TestReportError:
    def test_on_error_called_with_canonical_label(self) -> None:
        captured: list[str] = []
        d = ControlChannelDecoder(on_error=captured.append)
        try:
            d.feed("\x10\x02not-hex-header:")  # bad header → protocol error
            d.finish()
        except ControlChannelProtocolError:
            pass
        # The label must be "control_channel_protocol_error" (exact string).
        assert "control_channel_protocol_error" in captured

    def test_on_error_not_called_on_happy_path(self) -> None:
        captured: list[str] = []
        d = ControlChannelDecoder(on_error=captured.append)
        d.feed("plain data\n")
        d.finish()
        assert captured == []

    def test_report_error_returns_protocol_error_instance(self) -> None:
        d = ControlChannelDecoder()
        err = d._report_error("custom message")
        assert isinstance(err, ControlChannelProtocolError)
        assert str(err) == "custom message"
