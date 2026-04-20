#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Frame-ordering invariant tests for ControlChannelDecoder.

The decoder must emit chunks in the exact ORDER they appear in the wire stream,
regardless of how feed() is called (whole, byte-at-a-time, or arbitrary splits).

Key invariants:
  - Control frames never arrive before data that preceded them on the wire.
  - Data that follows a control frame never arrives before that control frame.
  - Adjacent DataChunks may be split across call boundaries — that is acceptable;
    what must not change is the *relative ordering* of data vs control segments.

The comparison helper ``merged_chunks()`` collapses adjacent DataChunks so that
split-vs-unsplit differences do not cause false failures.
"""

from __future__ import annotations

import pytest

from provide.terminal.control_channel import (
    DLE,
    ControlChannelChunk,
    ControlChannelDecoder,
    ControlChunk,
    DataChunk,
    encode_control,
    encode_data,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def collect_chunks(stream: str, chunk_sizes: list[int]) -> list[ControlChannelChunk]:
    """Feed *stream* to a fresh decoder in pieces defined by *chunk_sizes*.

    Returns the concatenation of all chunks emitted by feed() across all calls.
    Sizes are consumed left-to-right; any remainder not covered is fed last.
    """
    decoder = ControlChannelDecoder()
    results: list[ControlChannelChunk] = []
    pos = 0
    for size in chunk_sizes:
        piece = stream[pos : pos + size]
        if piece:
            results.extend(decoder.feed(piece))
        pos += size
        if pos >= len(stream):
            break
    if pos < len(stream):
        results.extend(decoder.feed(stream[pos:]))
    return results


def merge_data_chunks(chunks: list[ControlChannelChunk]) -> list[ControlChannelChunk]:
    """Collapse adjacent DataChunks into a single DataChunk.

    The decoder may legitimately emit multiple DataChunks for a single logical
    data segment when feed() is called at a mid-data boundary.  For ordering
    invariant checks we compare *merged* sequences so that split-chunk noise
    does not produce false failures.
    """
    merged: list[ControlChannelChunk] = []
    for chunk in chunks:
        if merged and isinstance(merged[-1], DataChunk) and isinstance(chunk, DataChunk):
            merged[-1] = DataChunk(merged[-1].data + chunk.data)
        else:
            merged.append(chunk)
    return merged


def assert_ordering_matches(chunks: list[ControlChannelChunk], expected: list[ControlChannelChunk]) -> None:
    """Assert that merged chunk sequence equals *expected*."""
    assert merge_data_chunks(chunks) == expected


# ---------------------------------------------------------------------------
# Fixtures / constants
# ---------------------------------------------------------------------------

DATA_A = encode_data("AAAA-data-alpha")
CTRL_B = encode_control({"type": "beta", "seq": 1})
DATA_C = encode_data("CCCC-data-charlie")
CTRL_D = encode_control({"type": "delta", "seq": 2})

BASELINE_STREAM = DATA_A + CTRL_B + DATA_C + CTRL_D

BASELINE_CHUNKS: list[ControlChannelChunk] = [
    DataChunk("AAAA-data-alpha"),
    ControlChunk({"type": "beta", "seq": 1}),
    DataChunk("CCCC-data-charlie"),
    ControlChunk({"type": "delta", "seq": 2}),
]


# ---------------------------------------------------------------------------
# 1. Baseline — whole stream in one feed()
# ---------------------------------------------------------------------------

def test_baseline_whole_feed() -> None:
    """Feeding the full stream at once yields the four chunks in order."""
    chunks = collect_chunks(BASELINE_STREAM, [len(BASELINE_STREAM)])
    assert_ordering_matches(chunks, BASELINE_CHUNKS)


# ---------------------------------------------------------------------------
# 2. Byte-level split invariant
# ---------------------------------------------------------------------------

def test_byte_by_byte_feed() -> None:
    """Feeding one byte at a time must produce the same ordered sequence."""
    chunks = collect_chunks(BASELINE_STREAM, [1] * len(BASELINE_STREAM))
    assert_ordering_matches(chunks, BASELINE_CHUNKS)


# ---------------------------------------------------------------------------
# 3. Arbitrary chunk boundaries (every possible split point)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("split", list(range(1, len(BASELINE_STREAM))))
def test_all_split_positions(split: int) -> None:
    """Every possible 2-piece split of the stream produces the baseline order."""
    chunks = collect_chunks(BASELINE_STREAM, [split])
    assert_ordering_matches(chunks, BASELINE_CHUNKS)


# ---------------------------------------------------------------------------
# 4. Control frame header split — splits inside DLE STX, length hex, ':', JSON
# ---------------------------------------------------------------------------

def _header_split_stream() -> str:
    """A simple stream: data + one control frame."""
    return encode_data("pre") + encode_control({"k": "v"})


def _header_split_expected() -> list[ControlChannelChunk]:
    return [DataChunk("pre"), ControlChunk({"k": "v"})]


@pytest.mark.parametrize("split_offset", [
    0,   # split before DLE (in the data portion)
    3,   # split on DLE itself (first byte of header)
    4,   # split between DLE and STX
    5,   # split after STX (inside first hex digit)
    7,   # split mid-length hex
    11,  # split after ':' separator
    13,  # split inside JSON body
])
def test_control_header_splits(split_offset: int) -> None:
    """Splits at various positions inside/around a control frame header."""
    stream = _header_split_stream()
    split = min(split_offset, len(stream) - 1)
    chunks = collect_chunks(stream, [split])
    assert_ordering_matches(chunks, _header_split_expected())


# ---------------------------------------------------------------------------
# 5. Data containing literal DLE byte — split between the two escaped DLEs
# ---------------------------------------------------------------------------

def test_data_with_dle_not_mistaken_for_control() -> None:
    """A literal DLE in data is escaped as DLE DLE; splitting between them is safe.

    Wire bytes for encode_data(x + DLE + y): x DLE DLE y
    Split after the first DLE so the two DLEs are in different feed() calls.
    The decoder must not mistake the first DLE for a control-frame start.
    """
    raw_data = f"x{DLE}y"
    stream = encode_data(raw_data)  # → x \x10 \x10 y on wire
    split = stream.index(DLE) + 1  # position just after first DLE
    chunks = collect_chunks(stream, [split])
    # Ordering invariant: all data arrives before any following control; no
    # reordering.  Content correctness: merged data must equal raw_data.
    merged = merge_data_chunks(chunks)
    assert len(merged) == 1
    assert isinstance(merged[0], DataChunk)
    assert merged[0].data == raw_data


def test_data_with_dle_byte_by_byte() -> None:
    """Escaped DLE sequence decoded correctly even when fed one byte at a time."""
    raw_data = f"hello{DLE}world{DLE}end"
    stream = encode_data(raw_data)
    chunks = collect_chunks(stream, [1] * len(stream))
    merged = merge_data_chunks(chunks)
    assert len(merged) == 1
    assert isinstance(merged[0], DataChunk)
    assert merged[0].data == raw_data


def test_data_with_dle_then_control() -> None:
    """DLE-escaped data followed by a control frame — ordering preserved."""
    raw_data = f"A{DLE}B"
    stream = encode_data(raw_data) + encode_control({"type": "marker"})
    expected = [DataChunk(raw_data), ControlChunk({"type": "marker"})]
    # Byte-at-a-time
    chunks = collect_chunks(stream, [1] * len(stream))
    assert_ordering_matches(chunks, expected)


# ---------------------------------------------------------------------------
# 6. Multiple control frames back-to-back (no data between)
# ---------------------------------------------------------------------------

def test_back_to_back_control_frames_whole() -> None:
    """Two adjacent control frames without data emit in order."""
    ctrl1 = encode_control({"type": "one"})
    ctrl2 = encode_control({"type": "two"})
    stream = ctrl1 + ctrl2
    chunks = collect_chunks(stream, [len(stream)])
    assert chunks == [ControlChunk({"type": "one"}), ControlChunk({"type": "two"})]


def test_back_to_back_control_frames_byte_by_byte() -> None:
    """Two adjacent control frames fed byte-at-a-time still emit in order."""
    ctrl1 = encode_control({"type": "one"})
    ctrl2 = encode_control({"type": "two"})
    stream = ctrl1 + ctrl2
    chunks = collect_chunks(stream, [1] * len(stream))
    assert chunks == [ControlChunk({"type": "one"}), ControlChunk({"type": "two"})]


def test_back_to_back_three_control_frames() -> None:
    """Three adjacent control frames emit in exact order."""
    stream = encode_control({"n": 1}) + encode_control({"n": 2}) + encode_control({"n": 3})
    chunks = collect_chunks(stream, [1] * len(stream))
    assert chunks == [ControlChunk({"n": 1}), ControlChunk({"n": 2}), ControlChunk({"n": 3})]


# ---------------------------------------------------------------------------
# 7. Interleaved many — 5 data + 5 control frames, byte-at-a-time
# ---------------------------------------------------------------------------

def test_interleaved_many_byte_by_byte() -> None:
    """Five data and five control frames interleaved, fed one byte at a time."""
    expected: list[ControlChannelChunk] = []
    stream = ""
    for i in range(5):
        data_text = f"data-segment-{i}"
        ctrl_payload = {"type": "ctrl", "i": i}
        stream += encode_data(data_text)
        stream += encode_control(ctrl_payload)
        expected.append(DataChunk(data_text))
        expected.append(ControlChunk(ctrl_payload))

    chunks = collect_chunks(stream, [1] * len(stream))
    assert_ordering_matches(chunks, expected)


# ---------------------------------------------------------------------------
# 8. Parametrised sweep over multiple chunk sizes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 7, 11, 16, 64, 1024])
def test_chunk_size_sweep(chunk_size: int) -> None:
    """Output ordering is identical for chunk sizes 1, 2, 3, 5, 7, 11, 16, 64, 1024."""
    sizes = [chunk_size] * (len(BASELINE_STREAM) // chunk_size + 2)
    chunks = collect_chunks(BASELINE_STREAM, sizes)
    assert_ordering_matches(chunks, BASELINE_CHUNKS)


# ---------------------------------------------------------------------------
# 9. Control never jumps ahead of preceding data (explicit ordering checks)
# ---------------------------------------------------------------------------

def test_control_does_not_precede_data_byte_by_byte() -> None:
    """Control chunk B must appear after ALL of data-A, never before any of it."""
    data_text = "some-data-before-control"
    stream = encode_data(data_text) + encode_control({"type": "after"})

    decoder = ControlChannelDecoder()
    saw_data_content = ""
    saw_control = False
    error_msg = None

    for i in range(len(stream)):
        for chunk in decoder.feed(stream[i : i + 1]):
            if isinstance(chunk, ControlChunk):
                saw_control = True
                if saw_data_content != data_text:
                    error_msg = (
                        f"Control arrived before data was complete: "
                        f"data so far={saw_data_content!r}, expected={data_text!r}"
                    )
            elif isinstance(chunk, DataChunk):
                if saw_control:
                    error_msg = f"Data arrived after control: {chunk.data!r}"
                saw_data_content += chunk.data

    assert error_msg is None, error_msg
    assert saw_data_content == data_text
    assert saw_control


def test_data_after_control_does_not_precede_control_byte_by_byte() -> None:
    """Data-C must appear after control-B, never before it."""
    ctrl_payload = {"type": "mid"}
    stream = encode_control(ctrl_payload) + encode_data("data-after-control")

    decoder = ControlChannelDecoder()
    saw_control = False
    saw_data_after = ""
    error_msg = None

    for i in range(len(stream)):
        for chunk in decoder.feed(stream[i : i + 1]):
            if isinstance(chunk, ControlChunk):
                saw_control = True
            elif isinstance(chunk, DataChunk):
                if not saw_control:
                    error_msg = f"Data arrived before control: {chunk.data!r}"
                saw_data_after += chunk.data

    assert error_msg is None, error_msg
    assert saw_control
    assert saw_data_after == "data-after-control"
