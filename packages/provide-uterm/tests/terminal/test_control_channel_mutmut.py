#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Surgical mutmut-killer tests for ``ControlFrameDecoder``.

Targets the high-density mutation areas: ``__init__`` default values,
the ``feed``/``finish``/``_drain``/``_try_parse_frame`` decoder hot path,
and ``_report_error`` callback invocation.
"""

from __future__ import annotations

import pytest

from provide.uterm.control_channel import (
    DLE,
    STX,
    ControlChunk,
    ControlFrameDecoder,
    ControlFrameProtocolError,
    DataChunk,
    _utf8_payload_end,
    encode_control_frame,
)

# ---------------------------------------------------------------------------
# __init__ defaults
# ---------------------------------------------------------------------------


class TestDecoderInit:
    def test_max_control_payload_bytes_default_is_one_meg(self) -> None:
        d = ControlFrameDecoder()
        assert d._max_control_payload_bytes == 1_048_576

    def test_max_buffer_bytes_default_is_ten_meg(self) -> None:
        d = ControlFrameDecoder()
        assert d._max_buffer_bytes == 10_485_760

    def test_max_control_payload_explicit_overrides_default(self) -> None:
        d = ControlFrameDecoder(max_control_payload_bytes=512)
        assert d._max_control_payload_bytes == 512

    def test_max_buffer_explicit_overrides_default(self) -> None:
        d = ControlFrameDecoder(max_buffer_bytes=2048)
        assert d._max_buffer_bytes == 2048

    def test_max_control_payload_min_clamp(self) -> None:
        """Values <= 0 clamp up to 1."""
        d = ControlFrameDecoder(max_control_payload_bytes=0)
        assert d._max_control_payload_bytes == 1

    def test_max_buffer_min_clamp(self) -> None:
        d = ControlFrameDecoder(max_buffer_bytes=-100)
        assert d._max_buffer_bytes == 1

    def test_buffer_starts_empty_string(self) -> None:
        d = ControlFrameDecoder()
        assert d._buffer == ""

    def test_buffer_parts_starts_empty_list(self) -> None:
        d = ControlFrameDecoder()
        assert d._buffer_parts == []

    def test_on_error_defaults_to_none(self) -> None:
        d = ControlFrameDecoder()
        assert d._on_error is None

    def test_on_error_stored_as_attribute(self) -> None:
        def cb(_: str) -> None:
            return None

        d = ControlFrameDecoder(on_error=cb)
        assert d._on_error is cb


# ---------------------------------------------------------------------------
# feed: type checking + buffer overflow + drain integration
# ---------------------------------------------------------------------------


class TestFeed:
    def test_non_string_chunk_raises_typeerror_with_actual_type(self) -> None:
        d = ControlFrameDecoder()
        with pytest.raises(TypeError) as exc_info:
            d.feed(b"bytes")  # type: ignore[arg-type]
        # Mutation that replaces ``type(chunk).__name__`` with
        # ``type(None).__name__`` (== 'NoneType') would put 'NoneType' in the
        # message instead of 'bytes'.
        assert "bytes" in str(exc_info.value), f"expected 'bytes' in {exc_info.value!r}"

    def test_non_string_chunk_type_is_not_nonetype_in_error(self) -> None:
        d = ControlFrameDecoder()
        with pytest.raises(TypeError) as exc_info:
            d.feed(42)  # type: ignore[arg-type]
        # If the mutation forces type(None) we'd see 'NoneType'; assert we don't.
        assert "NoneType" not in str(exc_info.value)
        assert "int" in str(exc_info.value)

    def test_buffer_overflow_raises_protocol_error(self) -> None:
        d = ControlFrameDecoder(max_buffer_bytes=10)
        with pytest.raises(ControlFrameProtocolError) as exc_info:
            d.feed("x" * 11)
        assert "overflow" in str(exc_info.value)

    def test_buffer_overflow_resets_state(self) -> None:
        d = ControlFrameDecoder(max_buffer_bytes=5)
        try:
            d.feed("toolong")
        except ControlFrameProtocolError:
            pass
        # State must be cleared after overflow.
        assert d._buffer == ""
        assert d._buffer_parts == []

    def test_feed_returns_pass_through_data_chunk(self) -> None:
        d = ControlFrameDecoder()
        events = d.feed("hello")
        assert len(events) == 1
        assert isinstance(events[0], DataChunk)
        assert events[0].data == "hello"

    def test_feed_decodes_complete_control_frame(self) -> None:
        d = ControlFrameDecoder()
        frame = encode_control_frame({"type": "test", "x": 1})
        events = d.feed(frame)
        controls = [e for e in events if isinstance(e, ControlChunk)]
        assert len(controls) == 1
        assert controls[0].control == {"type": "test", "x": 1}

    def test_feed_buffers_incomplete_control_frame(self) -> None:
        d = ControlFrameDecoder()
        frame = encode_control_frame({"type": "split"})
        events1 = d.feed(frame[:5])
        events2 = d.feed(frame[5:])
        # The combined feed should yield exactly one control event.
        all_controls = [e for e in events1 + events2 if isinstance(e, ControlChunk)]
        assert len(all_controls) == 1
        assert all_controls[0].control == {"type": "split"}

    def test_feed_buffer_parts_collapsed_after_drain(self) -> None:
        d = ControlFrameDecoder()
        d.feed("complete data\n")
        # After a successful feed with no unconsumed data, _buffer_parts is empty.
        assert d._buffer_parts == []

    def test_feed_protocol_error_clears_state_exactly(self) -> None:
        d = ControlFrameDecoder()
        with pytest.raises(ControlFrameProtocolError) as exc_info:
            d.feed(f"{DLE}x")
        assert str(exc_info.value) == "invalid control prefix"
        assert d._buffer == ""
        assert d._buffer_parts == []


# ---------------------------------------------------------------------------
# finish: truncated frame detection + final-flag propagation
# ---------------------------------------------------------------------------


class TestFinish:
    def test_finish_on_empty_buffer_returns_empty(self) -> None:
        d = ControlFrameDecoder()
        assert d.finish() == []

    def test_finish_with_truncated_control_frame_raises(self) -> None:
        """A control-frame prefix without complete header is truncated."""
        d = ControlFrameDecoder()
        # Feed only the DLE STX prefix (2 chars). Not enough for header bytes.
        d.feed("\x10\x02")
        with pytest.raises(ControlFrameProtocolError) as exc:
            d.finish()
        assert str(exc.value) == "truncated control frame"

    def test_finish_resets_state_on_protocol_error(self) -> None:
        d = ControlFrameDecoder()
        d.feed("\x10\x02")
        try:
            d.finish()
        except ControlFrameProtocolError:
            pass
        assert d._buffer == ""
        assert d._buffer_parts == []

    def test_finish_drains_remaining_data_chunk(self) -> None:
        d = ControlFrameDecoder()
        # Plain data without a DLE STX prefix — yielded as DataChunk.
        events = d.feed("trailing")
        # finish() with empty buffer must succeed.
        assert d.finish() == []
        assert any(isinstance(e, DataChunk) for e in events)

    def test_finish_residual_buffer_clears_state_exactly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        d = ControlFrameDecoder()
        d._buffer = "leftover"
        d._buffer_parts = ["leftover"]
        monkeypatch.setattr(d, "_drain", lambda *, final: [])

        with pytest.raises(ControlFrameProtocolError) as exc_info:
            d.finish()

        assert str(exc_info.value) == "truncated control frame"
        assert d._buffer == ""
        assert d._buffer_parts == []


class TestProtocolErrorMessages:
    def test_invalid_json_message_is_exact(self) -> None:
        d = ControlFrameDecoder()
        raw = f"{DLE}{STX}00000008:not-json"
        with pytest.raises(ControlFrameProtocolError) as exc_info:
            d.feed(raw)
        assert str(exc_info.value) == "invalid control json"

    def test_non_object_payload_message_is_exact(self) -> None:
        d = ControlFrameDecoder()
        raw = f"{DLE}{STX}00000002:[]"
        with pytest.raises(ControlFrameProtocolError) as exc_info:
            d.feed(raw)
        assert str(exc_info.value) == "control payload must be an object"

    def test_invalid_utf8_length_message_is_exact(self) -> None:
        d = ControlFrameDecoder()
        payload = '{"emoji":"😀"}'
        declared_bytes = payload.encode("utf-8").index("😀".encode()) + 1
        raw = f"{DLE}{STX}{declared_bytes:08x}:{payload}"
        with pytest.raises(ControlFrameProtocolError) as exc_info:
            d.feed(raw)
        assert str(exc_info.value) == "invalid control payload length"

    def test_incomplete_header_message_is_exact(self) -> None:
        d = ControlFrameDecoder()
        d.feed(f"{DLE}{STX}0000")
        with pytest.raises(ControlFrameProtocolError) as exc_info:
            d.finish()
        assert str(exc_info.value) == "truncated control frame"

    def test_invalid_header_message_is_exact(self) -> None:
        d = ControlFrameDecoder()
        with pytest.raises(ControlFrameProtocolError) as exc_info:
            d.feed(f"{DLE}{STX}zzzzzzzz:{{}}")
        assert str(exc_info.value) == "invalid control header"

    def test_payload_too_large_message_is_exact(self) -> None:
        d = ControlFrameDecoder(max_control_payload_bytes=5)
        with pytest.raises(ControlFrameProtocolError) as exc_info:
            d.feed(encode_control_frame({"k": "v"}))
        assert str(exc_info.value) == "control payload too large"

    def test_incomplete_payload_message_is_exact(self) -> None:
        d = ControlFrameDecoder()
        d.feed(f"{DLE}{STX}00000008:{{")
        with pytest.raises(ControlFrameProtocolError) as exc_info:
            d.finish()
        assert str(exc_info.value) == "truncated control frame"

    def test_trailing_dle_message_is_exact(self) -> None:
        d = ControlFrameDecoder()
        d.feed(DLE)
        with pytest.raises(ControlFrameProtocolError) as exc_info:
            d.finish()
        assert str(exc_info.value) == "truncated control frame"

    def test_invalid_prefix_message_is_exact(self) -> None:
        d = ControlFrameDecoder()
        with pytest.raises(ControlFrameProtocolError) as exc_info:
            d.feed(f"{DLE}x")
        assert str(exc_info.value) == "invalid control prefix"

    def test_empty_control_payload_uses_invalid_json_error(self) -> None:
        d = ControlFrameDecoder()
        with pytest.raises(ControlFrameProtocolError) as exc_info:
            d.feed(f"{DLE}{STX}00000000:")
        assert str(exc_info.value) == "invalid control json"


# ---------------------------------------------------------------------------
# _report_error: on_error callback invocation
# ---------------------------------------------------------------------------


class TestReportError:
    def test_on_error_called_with_canonical_label(self) -> None:
        captured: list[str] = []
        d = ControlFrameDecoder(on_error=captured.append)
        try:
            d.feed("\x10\x02not-hex-header:")  # bad header → protocol error
            d.finish()
        except ControlFrameProtocolError:
            pass
        # The label must be "control_channel_protocol_error" (exact string).
        assert "control_frame_protocol_error" in captured

    def test_on_error_not_called_on_happy_path(self) -> None:
        captured: list[str] = []
        d = ControlFrameDecoder(on_error=captured.append)
        d.feed("plain data\n")
        d.finish()
        assert captured == []

    def test_report_error_returns_protocol_error_instance(self) -> None:
        d = ControlFrameDecoder()
        err = d._report_error("custom message")
        assert isinstance(err, ControlFrameProtocolError)
        assert str(err) == "custom message"


# ---------------------------------------------------------------------------
# _utf8_payload_end — the byte-length walk over a character buffer
# ---------------------------------------------------------------------------


class TestUtf8PayloadEndBounds:
    """Kills the loop's two bounds and the length-error message.

    The walk advances a character index while counting UTF-8 *bytes*, so its
    guard has to hold both ends at once: stop at the end of the buffer, and
    stop once the declared byte count is reached. Each half is load-bearing in
    a different direction, and neither was pinned.
    """

    def test_a_short_buffer_returns_none_rather_than_reading_past_it(self) -> None:
        """Kills `and` -> `or` and `idx < len(buf)` -> `<=`.

        Both let the walk step past the last character when the buffer is
        shorter than the declared length — the case that says "not yet, feed me
        more", which is the normal state of a frame split across two reads.
        """
        assert _utf8_payload_end("ab", 0, 5) is None

    def test_the_walk_stops_on_the_byte_it_was_asked_for(self) -> None:
        """Kills `byte_count < payload_bytes` -> `<=`.

        A buffer LONGER than the payload is what separates the two: at an exact
        fit the mutant takes one more character, overshoots, and reports the
        length invalid. With a buffer that ends exactly at the payload the loop
        stops for the other reason and the mutant survives.
        """
        assert _utf8_payload_end("abcd", 0, 3) == 3

    def test_a_length_splitting_a_code_point_is_named_as_such(self) -> None:
        """Kills the message -> None, -> XX-wrapped, -> upper-cased.

        A length landing mid-character cannot be fixed by feeding more text, so
        this is the one payload-length error the sender has to be told apart
        from "incomplete". é is two bytes; asking for one splits it.
        """
        with pytest.raises(ControlFrameProtocolError) as excinfo:
            _utf8_payload_end("é", 0, 1)
        assert str(excinfo.value) == "invalid control payload length"


class TestFinishResetsEveryBuffer:
    """Kills the three buffer resets on the error path of ``finish``.

    ``finish`` rejects a truncated frame, and on the way out clears the text
    buffer, the parts list and the byte buffer. Nothing asserted any of them,
    so each could be set to None — leaving a decoder that raises AttributeError
    or TypeError on the next feed instead of starting clean. A decoder that
    cannot be reused after one bad frame is a worse failure than the bad frame.
    """

    @staticmethod
    def _truncated() -> ControlFrameDecoder:
        decoder = ControlFrameDecoder()
        decoder.feed(DLE + STX + "0000000a:")  # header promising ten bytes, none sent
        return decoder

    def test_a_rejected_frame_leaves_every_buffer_empty(self) -> None:
        decoder = self._truncated()
        with pytest.raises(ControlFrameProtocolError):
            decoder.finish()
        assert decoder._buffer == ""
        assert decoder._buffer_parts == []
        assert decoder._buffer_bytes == bytearray()

    def test_the_decoder_still_works_after_a_rejected_frame(self) -> None:
        """The resets exist so the next frame decodes; assert that, not just the state."""
        decoder = self._truncated()
        with pytest.raises(ControlFrameProtocolError):
            decoder.finish()
        assert decoder.feed(encode_control_frame({"type": "ping"})) == [ControlChunk({"type": "ping"})]


class TestDecodeDataPartsHonoursItsDeclaredArgument:
    """Kills the two branches of ``_decode_data_parts`` the decoder never takes.

    The signature says ``Sequence[memoryview | bytes]`` and the body has an
    empty-input branch, but every in-module call site passes a non-empty list of
    memoryviews. So the ``bytes`` branch and the empty branch are reachable only
    through the declared contract — which left ``part.decode("utf-8")`` free to
    become ``decode(None)`` and the empty case free to return anything at all.
    """

    def test_no_parts_decode_to_the_empty_string(self) -> None:
        assert ControlFrameDecoder._decode_data_parts([]) == ""

    def test_a_lone_bytes_part_decodes_as_utf8(self) -> None:
        """Kills `decode(None)` and `decode("XXutf-8XX")` — both raise here."""
        assert ControlFrameDecoder._decode_data_parts([b"h\xc3\xa9llo"]) == "héllo"


class TestDrainAlwaysMovesTheScanForward:
    """Kills `idx += 1` -> `idx = 1` and `idx += 2` -> `idx -= 2`.

    Both send the scan somewhere it has already been, and on almost every input
    that means an unbounded spin rather than a wrong answer — a timeout, which
    only counts as a kill by luck of test ordering. Each case below is one of
    the few inputs that terminates under its mutant, so the failure is an
    assertion rather than a hang. Both inputs also terminate under the *other*
    mutant, so neither test can be the one that wedges the other's run.
    """

    def test_a_trailing_escaped_dle_is_one_literal_dle(self) -> None:
        """Kills `idx += 2` -> `idx -= 2`: the backwards jump walks off the buffer."""
        decoder = ControlFrameDecoder()
        assert decoder.feed(DLE + DLE) == [DataChunk(DLE)]

    def test_an_escaped_dle_followed_by_stx_is_data_not_a_frame(self) -> None:
        """Kills `idx += 1` -> `idx = 1`: the reset lands back on the escape's
        second DLE and reads the STX after it as a frame prefix, dropping it."""
        decoder = ControlFrameDecoder()
        assert decoder.feed(DLE + DLE + STX) == [DataChunk(DLE + STX)]


class TestFrameEndIsPastThePayloadNotBeforeIt:
    """Kills `start + payload_bytes` -> `start - payload_bytes` in the frame end.

    Subtracting sends the parse offset backwards, and for any normal payload it
    lands before the frame start and re-parses the same frame forever — another
    timeout rather than a kill. A payload shorter than the header walks the
    offset back *into* the header instead, which terminates and leaves the tail
    of the header as trailing data.
    """

    def test_the_helper_returns_the_offset_after_the_payload(self) -> None:
        """Asserted on the helper directly, because ``_drain`` cannot reach it fast.

        Every frame whose payload is at least header-sized sends the offset
        behind the frame start, and the scan re-parses the same frame forever —
        so the tests that go through ``feed`` hang instead of failing, and the
        mutant is reported as a timeout rather than killed. Calling the helper
        skips the loop entirely.
        """
        decoder = ControlFrameDecoder()
        assert decoder._payload_end_for_utf8_length(b'{"a":1}', 0, 7) == (7, '{"a":1}')

    def test_a_minimal_frame_decodes_to_one_chunk_and_no_trailing_data(self) -> None:
        """A payload shorter than the header is the one size that terminates.

        The offset lands back inside the header rather than before the frame,
        so the scan finishes and leaves the tail of the header as trailing data.
        """
        decoder = ControlFrameDecoder()
        assert decoder.feed(encode_control_frame({})) == [ControlChunk({})]


class TestTheWholeLengthFieldIsValidated:
    """Kills `buf[idx + 2 : idx + 10]` -> `buf[idx + 3 : idx + 10]`.

    Dropping the first of the eight length digits does not change the parsed
    value — the field is zero-padded and no frame is 0x10000000 bytes — so the
    only thing it changes is that the dropped digit stops being checked for
    being hex at all. A junk first digit is then read as a valid frame.
    """

    def test_a_non_hex_first_length_digit_is_rejected(self) -> None:
        decoder = ControlFrameDecoder()
        with pytest.raises(ControlFrameProtocolError) as excinfo:
            decoder.feed(DLE + STX + "z0000002:{}")
        assert str(excinfo.value) == "invalid control header"


class TestOverflowNamesTheSizeAndLeavesAUsableDecoder:
    """Kills `buffered_bytes = len(...)` -> None and the byte-buffer reset -> None.

    The overflow error exists to tell the operator how far over the limit the
    peer went, so the count in the message is the whole point of computing it.
    And the reset beside it is what lets the decoder keep working afterwards:
    set to None, the next feed dies in ``bytearray.extend`` instead.
    """

    def test_the_message_carries_the_buffered_size(self) -> None:
        decoder = ControlFrameDecoder(max_buffer_bytes=5)
        with pytest.raises(ControlFrameProtocolError) as excinfo:
            decoder.feed("toolong")
        assert str(excinfo.value) == "control frame buffer overflow: 7 > 5"

    def test_the_decoder_still_decodes_after_an_overflow(self) -> None:
        decoder = ControlFrameDecoder(max_buffer_bytes=5)
        with pytest.raises(ControlFrameProtocolError):
            decoder.feed("toolong")
        assert decoder._buffer_bytes == bytearray()
        assert decoder.feed("o") == [DataChunk("o")]


class TestDrainKeepsTheUnconsumedTail:
    """Kills the three buffer assignments at the end of ``_drain``.

    ``_drain`` ends by either clearing the buffer or re-seating it on the bytes
    it could not consume. Nothing asserted either arm, so the text buffer could
    be set to None in both, and the parts list could be dropped entirely — the
    tail would still be decoded on the next feed, because the byte buffer is the
    one the parser reads. These pin the other two so they stay in step with it.
    """

    def test_a_fully_consumed_feed_clears_the_text_buffer(self) -> None:
        decoder = ControlFrameDecoder()
        decoder.feed("a")
        assert decoder._buffer == ""
        assert decoder._buffer_parts == []

    def test_an_unconsumed_tail_is_kept_as_text_and_as_one_part(self) -> None:
        decoder = ControlFrameDecoder()
        decoder.feed("a" + DLE + STX)
        assert decoder._buffer == DLE + STX
        assert decoder._buffer_parts == [DLE + STX]
        assert decoder._buffer_bytes == bytearray((DLE + STX).encode())


class TestWhichLayerReportsATruncatedFrame:
    """Kills `final=True` -> False/None in ``finish`` and in ``_drain``'s
    ``_try_parse_frame`` call.

    Both spellings end in the same ``truncated control frame`` error, so the
    exception alone cannot tell them apart: with ``final`` falsy the parser
    reports the frame as merely incomplete, ``_drain`` re-seats the tail, and
    ``finish``'s residual check raises instead. The difference is *which* layer
    decides, and the error hook can see it — it is called with the bytes still
    buffered when the parser rejects the frame, and after the residual branch
    has already cleared them when the fallback does.
    """

    def test_the_parser_rejects_it_while_the_bytes_are_still_buffered(self) -> None:
        seen: list[bytes] = []

        def note(_code: str) -> None:
            seen.append(bytes(decoder._buffer_bytes))

        decoder = ControlFrameDecoder(on_error=note)
        decoder.feed("a" + DLE + STX)
        with pytest.raises(ControlFrameProtocolError):
            decoder.finish()
        assert seen == [(DLE + STX).encode()]

    def test_the_residual_branch_clears_the_buffers_before_it_reports(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Kills the same three resets ``TestFinishResetsEveryBuffer`` covers.

        That test reads the buffers after ``finish`` returns, by which point the
        ``except`` clause has re-run identical resets and hidden any change. The
        hook is the only observer that runs between the two.
        """
        seen: list[tuple[str, list[str], bytearray]] = []

        def note(_code: str) -> None:
            seen.append((decoder._buffer, decoder._buffer_parts, decoder._buffer_bytes))

        decoder = ControlFrameDecoder(on_error=note)
        decoder._buffer = "leftover"
        decoder._buffer_parts = ["leftover"]
        monkeypatch.setattr(decoder, "_drain", lambda *, final: [])

        with pytest.raises(ControlFrameProtocolError):
            decoder.finish()
        assert seen == [("", [], bytearray())]


class TestFeedClearsTheByteBufferOnAProtocolError:
    """Kills the ``_buffer_bytes`` reset in ``feed``'s except clause -> None.

    ``feed`` clears three buffers when the drain rejects the stream.
    ``test_feed_protocol_error_clears_state_exactly`` asserts the text buffer
    and the parts list, but not the byte buffer — the only one of the three the
    parser actually reads. Set to None it passes every existing assertion and
    then dies in ``bytearray.extend`` on the next feed, so one bad chunk would
    take the connection down instead of one frame.
    """

    def test_a_rejected_chunk_leaves_a_usable_decoder(self) -> None:
        decoder = ControlFrameDecoder()
        with pytest.raises(ControlFrameProtocolError):
            decoder.feed(DLE + "x")
        assert decoder._buffer_bytes == bytearray()
        assert decoder.feed("o") == [DataChunk("o")]


class TestDrainRejectsAParseThatDoesNotAdvance:
    """Covers ``_drain``'s bound, which no real input can reach.

    Every offset update in the loop is a strict increase — ``idx += 1`` in the
    scan, ``idx += 2`` over an escaped DLE, and a frame end of at least
    ``idx + _HEADER_BYTES`` — so a correct decoder always walks off the end of
    the buffer well inside the bound. The bound is there for the case where one
    of those stops being true: without it the loop spins inside the caller's
    read loop, accumulating parts and events on every pass, and the only thing
    that ends it is the process. Reaching it takes a parser that reports a frame
    without consuming one.
    """

    def test_a_frame_end_that_stands_still_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {"type": "ping"}
        decoder = ControlFrameDecoder()
        monkeypatch.setattr(
            decoder,
            "_try_parse_frame",
            lambda _buf, idx, _buf_len, *, final: (ControlChunk(payload), idx),
        )
        with pytest.raises(ControlFrameProtocolError) as excinfo:
            decoder.feed(encode_control_frame(payload))
        assert str(excinfo.value) == "control frame parse did not advance"

    def test_a_frame_end_that_goes_backwards_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Standing still and moving backwards are separate failures of the same
        invariant; the bound has to catch both, and only the second distinguishes
        a bound that tests for equality from one that tests for progress."""
        payload = {"type": "ping"}
        decoder = ControlFrameDecoder()
        monkeypatch.setattr(
            decoder,
            "_try_parse_frame",
            lambda _buf, idx, _buf_len, *, final: (ControlChunk(payload), max(0, idx - 1)),
        )
        with pytest.raises(ControlFrameProtocolError) as excinfo:
            decoder.feed(encode_control_frame(payload))
        assert str(excinfo.value) == "control frame parse did not advance"

    def test_a_one_byte_buffer_stays_inside_the_bound(self) -> None:
        """Pins the bound at ``buf_len + 1``.

        The shortest buffer is the tight case, not the longest: the scan jumps
        to the next DLE rather than stepping, so any run of plain data takes two
        passes — one to cross it, one to see the end. At one byte the bound is
        exactly those two, and ``buf_len - 1`` is zero passes, which reports a
        single ordinary byte of output as a decoder that never advanced.
        """
        decoder = ControlFrameDecoder()
        assert decoder.feed("x") == [DataChunk("x")]

    def test_plain_data_with_no_frame_in_it_is_one_chunk(self) -> None:
        """Covers the miss arm of the jump: no DLE ahead, so the scan goes
        straight to the end of the buffer rather than reporting a frame at -1."""
        decoder = ControlFrameDecoder()
        assert decoder.feed("x" * 64) == [DataChunk("x" * 64)]
