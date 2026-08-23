#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Kill suite for the ``ControlFrameDecoder`` mutants the existing suites miss.

The 2026-08-15 full-perimeter sweep left ``control_channel.py`` at 89.94% --
the only red leg of thirty-seven -- with every survivor inside this decoder.
The three wired control-channel suites exercise the decoder heavily, so the
gap is not coverage: it is that they assert what the decoder RETURNS and not
the boundaries it returns it at, so off-by-one loop bounds, swapped defaults
and error-message text all survive.

This file targets exactly those. Each test names the mutant family it kills.
Adding a decoder suite means adding it to ``pytest_add_cli_args_test_selection``
in the root pyproject; control_channel.py is not inside a scoped mutation
group, so unlike the bridge-hub perimeter that is the only wiring needed.
"""

from __future__ import annotations

import pytest

from provide.uterm.control_channel import (
    DLE,
    STX,
    ControlFrameDecoder,
    ControlFrameProtocolError,
    _utf8_payload_end,
    encode_control_frame,
)

# ---------------------------------------------------------------------------
# _utf8_payload_end -- the shared scanner behind is_control_frame
# ---------------------------------------------------------------------------


class TestUtf8PayloadEnd:
    """Kills the loop-bound and error-text mutants in the module-level scanner.

    The loop is ``while idx < len(buf) and byte_count < payload_bytes``. Both
    comparisons and the conjunction are mutable, and each mutation fails only
    on an input that runs the scanner off the end of the buffer -- which is
    precisely the "declared more bytes than arrived" case the function exists
    to report as "not yet complete".
    """

    def test_a_short_buffer_returns_none_rather_than_running_off_the_end(self) -> None:
        """Kills `and` -> `or` and `idx < len(buf)` -> `idx <= len(buf)`.

        Both mutations keep the loop alive once ``idx`` reaches the end of the
        buffer, so ``buf[idx]`` raises IndexError instead of the function
        reporting the frame as incomplete.
        """
        assert _utf8_payload_end("ab", 0, 5) is None

    def test_the_scan_stops_as_soon_as_the_declared_bytes_are_seen(self) -> None:
        """Kills `byte_count < payload_bytes` -> `byte_count <= payload_bytes`.

        With ``<=`` the scanner consumes one character beyond the declared
        length, trips its own over-run guard, and raises on a frame that is
        perfectly well formed. Needs a buffer with a character to spare --
        with an exact-fit buffer the outer bound hides the mutation.
        """
        assert _utf8_payload_end("abc", 0, 2) == 2

    def test_a_length_that_splits_a_code_point_names_itself(self) -> None:
        """Kills the three mutations of the "invalid control payload length" text.

        'é' is two UTF-8 bytes, so a declared length of 1 ends mid-character.
        No amount of further input can fix that, which is why this is an error
        rather than a "not yet complete" -- and why the message is worth
        pinning.
        """
        with pytest.raises(ControlFrameProtocolError) as excinfo:
            _utf8_payload_end("é", 0, 1)
        assert str(excinfo.value) == "invalid control payload length"


# ---------------------------------------------------------------------------
# _decode_data_parts -- the zero/one/many join
# ---------------------------------------------------------------------------


class TestDecodeDataParts:
    """Kills the empty-result literal and the single-``bytes`` decode arguments.

    Three shapes exist (no parts, one part, many parts) and the one-part shape
    splits again on memoryview vs bytes. The decoder itself only ever produces
    memoryview parts, so the bytes branch is reachable only by calling the
    static helper directly -- which is why its mutants outlived a suite that
    drives everything through feed().
    """

    def test_no_parts_decodes_to_the_empty_string(self) -> None:
        """Kills `return ""` -> `return "XXXX"`."""
        assert ControlFrameDecoder._decode_data_parts([]) == ""

    def test_a_single_bytes_part_decodes_as_utf8(self) -> None:
        """Kills `part.decode(None)` and `part.decode("XXutf-8XX")`.

        Both raise -- TypeError and LookupError -- on the only input that
        reaches this branch.
        """
        assert ControlFrameDecoder._decode_data_parts([b"hi \xc3\xa9"]) == "hi é"

    def test_a_single_memoryview_part_takes_the_other_branch(self) -> None:
        """Guards the branch above: the memoryview arm must stay separate."""
        assert ControlFrameDecoder._decode_data_parts([memoryview(b"hi")]) == "hi"

    def test_many_parts_are_merged_before_decoding(self) -> None:
        """A code point split across parts decodes only if merged first."""
        assert ControlFrameDecoder._decode_data_parts([b"\xc3", b"\xa9"]) == "é"


# ---------------------------------------------------------------------------
# _try_parse_frame -- the eight-digit length header
# ---------------------------------------------------------------------------


class TestFrameHeaderValidation:
    """Kills `buf[idx + 2 : idx + 10]` -> `buf[idx + 3 : idx + 10]`.

    A seven-digit slice still parses: every legal payload length is under
    0x10000000, so its leading hex digit is '0' and dropping it leaves the
    value unchanged. What it does NOT do is validate that first digit, so the
    mutation is observable only through a header whose first hex character is
    junk -- exactly the malformed input the check exists for.
    """

    def test_a_non_hex_first_length_digit_is_rejected(self) -> None:
        decoder = ControlFrameDecoder()
        with pytest.raises(ControlFrameProtocolError) as excinfo:
            decoder.feed(f"{DLE}{STX}G0000002:{{}}")
        assert str(excinfo.value) == "invalid control header"

    def test_a_valid_eight_digit_header_still_parses(self) -> None:
        """Guard for the test above: the digit it corrupts must otherwise work."""
        decoder = ControlFrameDecoder()
        events = decoder.feed(encode_control_frame({"type": "ok"}))
        assert [e.control for e in events] == [{"type": "ok"}]


# ---------------------------------------------------------------------------
# feed -- buffer overflow reporting and reset
# ---------------------------------------------------------------------------


class TestFeedOverflow:
    """Kills the overflow message's byte count and the buffer reset it performs."""

    def test_the_overflow_error_reports_the_actual_buffered_size(self) -> None:
        """Kills `buffered_bytes = len(self._buffer_bytes)` -> `= None`.

        The number is the entire diagnostic value of this error: "overflow"
        without it cannot distinguish a slightly-too-large frame from a
        runaway stream.
        """
        decoder = ControlFrameDecoder(max_buffer_bytes=4)
        with pytest.raises(ControlFrameProtocolError) as excinfo:
            decoder.feed("abcdefg")
        assert str(excinfo.value) == "control frame buffer overflow: 7 > 4"

    def test_a_protocol_error_mid_stream_also_leaves_it_empty(self) -> None:
        """Kills `self._buffer_bytes = bytearray()` -> `= None` in feed's handler.

        Distinct from the overflow reset above: this is the `except
        ControlFrameProtocolError` arm, reached when _drain rejects the bytes
        rather than when they merely exceed the cap. Both arms reset, and only
        an input that raises INSIDE the drain exercises this one.
        """
        decoder = ControlFrameDecoder()
        with pytest.raises(ControlFrameProtocolError) as excinfo:
            decoder.feed(f"{DLE}x")
        assert str(excinfo.value) == "invalid control prefix"
        assert decoder._buffer_bytes == bytearray()
        assert [e.data for e in decoder.feed("ok")] == ["ok"]

    def test_overflow_leaves_the_decoder_empty_and_reusable(self) -> None:
        """Kills `self._buffer_bytes = bytearray()` -> `= None` on the overflow path.

        The reset is what makes a decoder survivable after a hostile peer: with
        None in its place the next feed() raises AttributeError instead of
        decoding, so the connection dies on the frame AFTER the bad one.
        """
        decoder = ControlFrameDecoder(max_buffer_bytes=4)
        with pytest.raises(ControlFrameProtocolError):
            decoder.feed("abcdefg")
        assert decoder._buffer_bytes == bytearray()
        assert [e.data for e in decoder.feed("ok")] == ["ok"]


# ---------------------------------------------------------------------------
# _drain / finish -- residual buffer bookkeeping
# ---------------------------------------------------------------------------


class TestResidualBufferState:
    """Kills the `_buffer` / `_buffer_parts` assignments on both drain exits.

    ``_drain`` ends one of two ways: everything consumed, or a tail left for
    the next feed. Both exits rewrite the decoder's shadow buffer state, and
    nothing downstream reads it -- so these assertions are the only thing
    standing between those assignments and silent rot.
    """

    def test_a_fully_consumed_buffer_clears_the_residual_state(self) -> None:
        """Kills `self._buffer = ""` -> `= None` on the consumed-everything exit."""
        decoder = ControlFrameDecoder()
        decoder.feed(encode_control_frame({"type": "ok"}))
        assert decoder._buffer == ""
        assert decoder._buffer_parts == []
        assert decoder._buffer_bytes == bytearray()

    def test_an_incomplete_frame_is_kept_verbatim_for_the_next_feed(self) -> None:
        """Kills `self._buffer = self._buffer_bytes.decode(...)` -> `= None`
        and `self._buffer_parts = [self._buffer]` -> `= None`.

        The tail is the half-frame the next chunk completes; losing it loses
        the frame.
        """
        decoder = ControlFrameDecoder()
        partial = f"{DLE}{STX}0000000a:"
        assert decoder.feed(partial) == []
        assert decoder._buffer == partial
        assert decoder._buffer_parts == [partial]

    def test_a_truncated_frame_at_finish_leaves_the_decoder_empty(self) -> None:
        """Kills `self._buffer_bytes = bytearray()` -> `= None` in finish's handler.

        finish() raising is the normal end of a truncated stream; the decoder
        must still be inspectable and reusable afterwards.
        """
        decoder = ControlFrameDecoder()
        decoder.feed(f"{DLE}{STX}0000000a:")
        with pytest.raises(ControlFrameProtocolError) as excinfo:
            decoder.finish()
        assert str(excinfo.value) == "truncated control frame"
        assert decoder._buffer_bytes == bytearray()
        assert decoder._buffer == ""
        assert decoder._buffer_parts == []


class TestFrameEndOffset:
    """Kills `return start + payload_bytes` -> `return start - payload_bytes`.

    The returned offset is where the decoder resumes after a frame. Negative
    or short, it does not crash -- it silently re-reads bytes it already
    consumed, so the damage shows up as the NEXT chunk being wrong rather than
    as an error. Only asserting what follows a frame catches it.
    """

    def test_data_after_a_frame_resumes_at_the_frame_end(self) -> None:
        decoder = ControlFrameDecoder()
        events = decoder.feed(encode_control_frame({"type": "ok"}) + "tail")
        assert [type(e).__name__ for e in events] == ["ControlChunk", "DataChunk"]
        assert events[0].control == {"type": "ok"}
        assert events[1].data == "tail"

    def test_two_frames_back_to_back_each_land_once(self) -> None:
        """A short offset would re-parse the first frame's tail as a second."""
        decoder = ControlFrameDecoder()
        stream = encode_control_frame({"n": 1}) + encode_control_frame({"n": 2})
        assert [e.control for e in decoder.feed(stream)] == [{"n": 1}, {"n": 2}]
