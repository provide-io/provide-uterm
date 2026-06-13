#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Hypothesis property tests for the inline control channel codec.

The control channel mixes raw terminal data and JSON control frames in a single
text stream. Property tests guard the two invariants we care about most:

1. Round-trip: encoding any value and feeding the result to a fresh decoder
   yields back the exact same logical chunks (a string payload as one
   ``DataChunk`` and a control dict as one ``ControlChunk``).
2. Robustness: feeding arbitrary text into a decoder either parses cleanly or
   raises ``ControlFrameProtocolError``; it must never crash with a different
   exception nor silently retain inconsistent buffered state.
"""

from __future__ import annotations

from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from provide.uterm.control_channel import (
    ControlChunk,
    ControlFrameDecoder,
    ControlFrameProtocolError,
    DataChunk,
    encode_control_frame,
    encode_terminal_data,
)

# Strategy for arbitrary JSON-compatible control payloads. Keys must be str
# (JSON object keys), values can be any recursively-nested JSON scalar/container.
_json_scalars = st.none() | st.booleans() | st.integers(min_value=-(2**31), max_value=2**31 - 1) | st.text()
_json_values = st.recursive(
    _json_scalars,
    lambda children: st.lists(children, max_size=5) | st.dictionaries(st.text(max_size=20), children, max_size=5),
    max_leaves=10,
)
_control_dicts = st.dictionaries(st.text(max_size=20), _json_values, max_size=5)

# Plain terminal data payloads (text, not bytes — the codec works on str).
_data_payloads = st.text(max_size=4096)

# Arbitrary text used to probe decoder robustness.
_arbitrary_text = st.text(max_size=4096)


@given(payload=_data_payloads)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_encode_data_round_trips_through_decoder(payload: str) -> None:
    """Any text payload survives encode_terminal_data -> decoder.feed -> finish."""
    decoder = ControlFrameDecoder()
    chunks = decoder.feed(encode_terminal_data(payload))
    chunks.extend(decoder.finish())
    if payload == "":
        assert chunks == []
        return
    # Decoder may emit data in multiple parts when escaped DLEs split it, but
    # joining them back must reproduce the original payload exactly.
    assert all(isinstance(c, DataChunk) for c in chunks)
    assert "".join(c.data for c in chunks if isinstance(c, DataChunk)) == payload


@given(payload=_control_dicts)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_encode_control_round_trips_through_decoder(payload: dict[str, Any]) -> None:
    """Any JSON-shaped dict survives encode_control_frame -> decoder.feed."""
    decoder = ControlFrameDecoder()
    chunks = decoder.feed(encode_control_frame(payload))
    chunks.extend(decoder.finish())
    assert len(chunks) == 1
    chunk = chunks[0]
    assert isinstance(chunk, ControlChunk)
    # JSON normalises some scalars (e.g. integer keys are forbidden, but our
    # strategy only produces str keys, so the round-trip should be exact).
    assert chunk.control == payload


@given(data=_data_payloads, payload=_control_dicts)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_mixed_data_and_control_round_trip(data: str, payload: dict[str, Any]) -> None:
    """Concatenated data + control frames decode in order."""
    decoder = ControlFrameDecoder()
    chunks = decoder.feed(encode_terminal_data(data) + encode_control_frame(payload))
    chunks.extend(decoder.finish())
    # Strip the empty leading DataChunk for the data == "" edge case.
    chunks = [c for c in chunks if not (isinstance(c, DataChunk) and c.data == "")]
    data_chunks = [c for c in chunks if isinstance(c, DataChunk)]
    control_chunks = [c for c in chunks if isinstance(c, ControlChunk)]
    if data:
        assert "".join(c.data for c in data_chunks) == data
    else:
        assert data_chunks == []
    assert len(control_chunks) == 1
    assert control_chunks[0].control == payload


@given(text=_arbitrary_text)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=300)
def test_decoder_never_crashes_on_arbitrary_input(text: str) -> None:
    """Arbitrary text either parses or raises ControlFrameProtocolError — nothing else."""
    decoder = ControlFrameDecoder()
    try:
        decoder.feed(text)
        decoder.finish()
    except ControlFrameProtocolError:
        # Allowed failure mode. Buffer must be cleared on protocol error.
        assert decoder._buffer == ""
        assert decoder._buffer_parts == []


@given(text=_arbitrary_text)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_decoder_chunked_feed_matches_single_feed(text: str) -> None:
    """Feeding text byte-by-byte produces the same chunks as a single feed,
    when neither path raises."""
    bulk_decoder = ControlFrameDecoder()
    chunked_decoder = ControlFrameDecoder()
    try:
        bulk_chunks = bulk_decoder.feed(text) + bulk_decoder.finish()
    except ControlFrameProtocolError:
        return  # Single-feed path rejects this input; chunked behaviour is then unconstrained.
    try:
        chunked_chunks: list[Any] = []
        for ch in text:
            chunked_chunks.extend(chunked_decoder.feed(ch))
        chunked_chunks.extend(chunked_decoder.finish())
    except ControlFrameProtocolError:
        return  # Acceptable: chunked path can fail differently on truncation boundaries.

    # Both paths succeeded — the *logical* contents must match. Note that
    # consecutive DataChunks coalesce differently across paths, so compare
    # by joining data and listing controls.
    bulk_data = "".join(c.data for c in bulk_chunks if isinstance(c, DataChunk))
    chunked_data = "".join(c.data for c in chunked_chunks if isinstance(c, DataChunk))
    bulk_controls = [c.control for c in bulk_chunks if isinstance(c, ControlChunk)]
    chunked_controls = [c.control for c in chunked_chunks if isinstance(c, ControlChunk)]
    assert bulk_data == chunked_data
    assert bulk_controls == chunked_controls
