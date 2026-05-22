#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Hypothesis property tests for the prompt detection engine.

Guarded invariants:

1. Robustness — feeding any random screen text into ``PromptDetector`` either
   returns a ``PromptMatch`` or ``None``; it never raises.
2. Determinism — the same snapshot fed twice yields the same outcome.
3. No-pattern detector — a detector with zero patterns must return ``None``
   for every input.
"""

from __future__ import annotations

import hashlib
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from provide.uterm.detection.detector import PromptDetector

_BASE_PATTERNS: list[dict[str, Any]] = [
    {"id": "prompt.login", "regex": r"Enter your name:", "input_type": "multi_key", "eol_pattern": "$"},
    {"id": "prompt.password", "regex": r"Password:", "input_type": "multi_key", "eol_pattern": "$"},
    {"id": "prompt.command", "regex": r"\$\s*$", "input_type": "multi_key", "eol_pattern": "$"},
    {"id": "prompt.yn", "regex": r"\[y/n\]\??\s*$", "input_type": "single_key", "eol_pattern": "$"},
]

_screen_text = st.text(max_size=2048)


def _snapshot(screen: str, *, cursor_at_end: bool = True) -> dict[str, Any]:
    return {
        "screen": screen,
        "screen_hash": hashlib.sha256(screen.encode()).hexdigest(),
        "cursor_at_end": cursor_at_end,
        "has_trailing_space": False,
        "cursor": {"y": screen.count("\n"), "x": 0},
        "captured_at": 1000.0,
    }


@given(screen=_screen_text)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_detector_never_raises_on_arbitrary_screen(screen: str) -> None:
    """detect_prompt accepts any screen text and returns a PromptMatch or None."""
    detector = PromptDetector(_BASE_PATTERNS)
    result = detector.detect_prompt(_snapshot(screen))
    assert result is None or result.prompt_id in {p["id"] for p in _BASE_PATTERNS}


@given(screen=_screen_text)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_detector_is_deterministic(screen: str) -> None:
    """Same snapshot fed twice yields identical match outcomes."""
    detector = PromptDetector(_BASE_PATTERNS)
    snap = _snapshot(screen)
    first = detector.detect_prompt(snap)
    second = detector.detect_prompt(snap)
    assert (first is None) == (second is None)
    if first is not None and second is not None:
        assert first.prompt_id == second.prompt_id
        assert first.input_type == second.input_type


@given(screen=_screen_text)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_empty_pattern_set_never_matches(screen: str) -> None:
    """A detector with zero patterns always returns None."""
    detector = PromptDetector([])
    assert detector.detect_prompt(_snapshot(screen)) is None


@given(screen=_screen_text, cursor_at_end=st.booleans())
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_diagnostics_match_is_consistent_with_detect_prompt(
    screen: str,
    cursor_at_end: bool,
) -> None:
    """detect_prompt_with_diagnostics().match must equal detect_prompt()."""
    detector = PromptDetector(_BASE_PATTERNS)
    snap = _snapshot(screen, cursor_at_end=cursor_at_end)
    match = detector.detect_prompt(snap)
    diag = detector.detect_prompt_with_diagnostics(snap)
    assert (match is None) == (diag.match is None)
    if match is not None and diag.match is not None:
        assert match.prompt_id == diag.match.prompt_id


@given(prefix=_screen_text)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_prefix_followed_by_known_prompt_matches(prefix: str) -> None:
    """Adding a known prompt at the end of any prefix yields *some* prompt match.

    The detector may select either the planted prompt or another pattern that
    happens to match elsewhere on the screen; both are acceptable. What we
    assert is that *something* matches — the planted prompt cannot be entirely
    lost by surrounding noise.
    """
    detector = PromptDetector(_BASE_PATTERNS)
    screen = prefix + "\nEnter your name:"
    result = detector.detect_prompt(_snapshot(screen))
    assert result is not None
    assert result.prompt_id in {p["id"] for p in _BASE_PATTERNS}
