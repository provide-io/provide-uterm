#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Coverage tests for control_stream.py missing lines."""

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
)


class TestDataChunkKindProperty:
    """Cover DataChunk.kind property (line 36)."""

    def test_kind_returns_data(self) -> None:
        """Covers line 36: DataChunk.kind returns 'data'."""
        chunk = DataChunk("hello")
        assert chunk.kind == "data"


class TestControlChunkKindProperty:
    """Cover ControlChunk.kind property (line 47)."""

    def test_kind_returns_control(self) -> None:
        """Covers line 47: ControlChunk.kind returns 'control'."""
        chunk = ControlChunk({"type": "ping"})
        assert chunk.kind == "control"


class TestFeedTypeError:
    """Cover feed() TypeError for non-str input (line 74)."""

    def test_feed_raises_for_non_str(self) -> None:
        """Covers line 74: TypeError raised when chunk is not str."""
        decoder = ControlStreamDecoder()
        with pytest.raises(TypeError, match="control stream chunks must be str"):
            decoder.feed(b"binary data")  # type: ignore[arg-type]

    def test_feed_raises_for_int(self) -> None:
        """Covers line 74: TypeError raised for int input."""
        decoder = ControlStreamDecoder()
        with pytest.raises(TypeError, match="control stream chunks must be str"):
            decoder.feed(123)  # type: ignore[arg-type]


class TestFinishWithRemainingBuffer:
    """Cover finish() raising error when buffer is non-empty after drain (line 82)."""

    def test_finish_raises_when_buffer_manually_set_after_drain(self) -> None:
        """Covers line 82: finish() raises if _buffer is non-empty after drain.

        We monkey-patch _drain to return without clearing the buffer,
        simulating residual data that _drain left behind.
        """
        from unittest.mock import patch

        decoder = ControlStreamDecoder()
        # Set buffer to non-empty value
        decoder._buffer = "leftover"

        # Patch _drain so it returns normally without clearing the buffer
        def fake_drain(*, final: bool) -> list:
            return []

        with (
            patch.object(decoder, "_drain", side_effect=fake_drain),
            pytest.raises(ControlStreamProtocolError, match="truncated control frame"),
        ):
            decoder.finish()


class TestDrainFinalDleAtEnd:
    """Cover _drain(final=True) when DLE is at end of buffer (lines 98-100)."""

    def test_final_true_raises_on_trailing_dle(self) -> None:
        """Covers lines 98-100: DLE at end with final=True raises truncated error."""
        decoder = ControlStreamDecoder()
        # DLE followed by nothing — idx+1 >= len means we need final check
        # In final=True mode, this should raise
        with pytest.raises(ControlStreamProtocolError, match="truncated control frame"):
            decoder.feed(DLE)
            decoder.finish()

    def test_feed_partial_dle_buffers_without_error(self) -> None:
        """Covers line 100: DLE at end with final=False just breaks (buffered)."""
        decoder = ControlStreamDecoder()
        # DLE alone in a feed (not final) — should NOT raise, should buffer
        result = decoder.feed(DLE)
        # No events emitted, DLE is buffered
        assert result == []

    def test_finish_raises_on_isolated_dle_in_buffer(self) -> None:
        """Covers lines 98-100: finish() with only DLE in buffer raises."""
        decoder = ControlStreamDecoder()
        decoder.feed(DLE)  # buffered
        with pytest.raises(ControlStreamProtocolError, match="truncated control frame"):
            decoder.finish()


class TestDrainFinalIncompleteHeader:
    """Cover _drain(final=True) when header is incomplete (lines 115-117)."""

    def test_final_true_raises_on_incomplete_header(self) -> None:
        """Covers lines 115-117: DLE+STX present but header incomplete with final=True."""
        decoder = ControlStreamDecoder()
        # Feed DLE+STX plus only a few header bytes (less than 11 total)
        partial = f"{DLE}{STX}0000"  # only 4 hex digits, need 8 + ':'
        decoder.feed(partial)
        with pytest.raises(ControlStreamProtocolError, match="truncated control frame"):
            decoder.finish()

    def test_feed_partial_header_buffers_without_error(self) -> None:
        """Covers line 117: incomplete header with final=False just buffers."""
        decoder = ControlStreamDecoder()
        partial = f"{DLE}{STX}0000"
        result = decoder.feed(partial)
        assert result == []


class TestDrainInvalidJson:
    """Cover _drain JSON decode error path (lines 137-138)."""

    def test_decoder_raises_on_invalid_json_payload(self) -> None:
        """Covers lines 137-138: json.JSONDecodeError wraps as ControlStreamProtocolError."""
        decoder = ControlStreamDecoder()
        # Manually construct a frame with invalid JSON
        bad_payload = "not-json!"
        length_hex = f"{len(bad_payload):08x}"
        raw = f"{DLE}{STX}{length_hex}:{bad_payload}"
        with pytest.raises(ControlStreamProtocolError, match="invalid control json"):
            decoder.feed(raw)


class TestDecoderEdgeCases:
    """Additional edge cases for _drain paths."""

    def test_data_before_control_then_more_data(self) -> None:
        """Covers data_parts flush when control frame starts (line 110-112)."""
        decoder = ControlStreamDecoder()
        raw = "before" + encode_control({"type": "ping"}) + "after"
        events = decoder.feed(raw)
        assert events[0] == DataChunk("before")
        assert events[1] == ControlChunk({"type": "ping"})
        assert events[2] == DataChunk("after")

    def test_finish_empty_buffer_returns_no_events(self) -> None:
        """Covers finish() with no buffered data — no error, no events."""
        decoder = ControlStreamDecoder()
        result = decoder.finish()
        assert result == []
