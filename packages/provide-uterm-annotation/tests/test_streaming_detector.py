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
    # Only the post-match tail is carried (here empty, as the key ends the
    # window), so the same key isn't re-reported on the next chunk.
    assert sd.detect("send", "nothing here", seq=3) == []


def test_second_split_pattern_after_a_hit_still_bridges() -> None:
    """A hit must not drop the post-match tail.

    Dropping the whole carry on any hit (the old behaviour) loses a *second*
    secret that begins right after a completed match and straddles the boundary.
    Here chunk 1 completes an escalation hit (``sudo``) and begins an AWS key;
    chunk 2 finishes the key, which must still be detected.
    """
    sd = StreamingDetector(PatternDetector())
    first = sd.detect("send", "sudo AKIA0123", seq=1)  # pragma: allowlist secret
    assert any(a.label == "privilege_escalation" for a in first)
    assert not any(a.label == "credential_exposure" for a in first)  # key not yet complete
    second = sd.detect("send", "456789AB ok", seq=2)  # pragma: allowlist secret
    assert any(a.label == "credential_exposure" for a in second), second


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
