#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for :class:`StreamingDetector` — boundary-split pattern detection."""

from __future__ import annotations

from provide.uterm.annotation import PatternDetector, StreamingDetector

# An AWS access key: literal ``AKIA`` + 12 uppercase/digits = 16 chars total.
_KEY = "AKIA0123456789AB"  # pragma: allowlist secret


def test_bare_detector_misses_a_split_pattern() -> None:
    """Documents the gap the wrapper closes: the stateless detector can't see across calls."""
    det = PatternDetector()
    assert det.detect("send", _KEY[:8], seq=1) == []
    assert det.detect("send", _KEY[8:], seq=2) == []  # second half alone never matches


def test_streaming_detector_catches_a_split_pattern() -> None:
    sd = StreamingDetector(PatternDetector())
    assert sd.detect("send", _KEY[:8], seq=1) == []  # incomplete in chunk 1
    out = sd.detect("send", _KEY[8:], seq=2)
    assert len(out) == 1
    assert out[0].label == "credential_exposure"
    # The match is owned by the chunk in which it completes.
    assert out[0].span.from_seq == 2


def test_whole_pattern_in_one_chunk_still_matches() -> None:
    sd = StreamingDetector(PatternDetector())
    out = sd.detect("send", f"export KEY={_KEY}", seq=5)
    assert len(out) == 1
    assert out[0].span.from_seq == 5


def test_no_reemit_on_the_following_chunk() -> None:
    sd = StreamingDetector(PatternDetector())
    sd.detect("send", _KEY[:8], seq=1)
    first = sd.detect("send", _KEY[8:], seq=2)
    assert len(first) == 1
    # The carry is dropped after a hit, so the same key isn't reported again.
    assert sd.detect("send", "nothing here", seq=3) == []


def test_empty_text_returns_empty_and_keeps_carry() -> None:
    sd = StreamingDetector(PatternDetector())
    sd.detect("send", _KEY[:8], seq=1)  # primes the carry
    assert sd.detect("send", "", seq=2) == []  # empty text short-circuits
    # carry survived the empty call, so the completion still matches
    assert len(sd.detect("send", _KEY[8:], seq=3)) == 1


def test_reset_drops_the_carry() -> None:
    sd = StreamingDetector(PatternDetector())
    sd.detect("send", _KEY[:8], seq=1)
    sd.reset()
    # After reset the first half is forgotten, so the completion can't bridge it.
    assert sd.detect("send", _KEY[8:], seq=2) == []


def test_carry_is_bounded() -> None:
    sd = StreamingDetector(PatternDetector(), max_carry=4)
    # First half is 8 chars but only 4 are retained, so the boundary can't be bridged.
    assert sd.detect("send", _KEY[:8], seq=1) == []
    assert sd.detect("send", _KEY[8:], seq=2) == []
