#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Surgical mutmut-killer tests for ``PromptDetector._detect_in_text``.

The ``_detect_in_text`` method has dense mutation coverage on the inner
loop's branching: negative-match short-circuit, ``expect_cursor_at_end``
check, ``cursor_miss_candidates`` accumulation, and the shape of the
``regex_matched_but_failed`` diagnostic dicts. Existing
``test_detector.py`` tests touch most of these via ``detect_prompt`` but
don't assert on the diagnostic dict keys directly — which is where many
mutmut mutations cluster.
"""

from __future__ import annotations

from typing import Any

from provide.uterm.detection.detector import PromptDetector


def _snap(
    screen: str,
    *,
    cursor_at_end: bool = True,
    has_trailing_space: bool = False,
    cursor_y: int = 0,
) -> dict[str, Any]:
    return {
        "screen": screen,
        "cursor_at_end": cursor_at_end,
        "has_trailing_space": has_trailing_space,
        "cursor": {"x": 0, "y": cursor_y},
    }


# ---------------------------------------------------------------------------
# Negative-match exclusion observable in diagnostics
# ---------------------------------------------------------------------------


class TestDetectInTextNegativeMatch:
    """Negative-match excludes a regex-hit and records ``reason=negative_match``."""

    def test_negative_match_excludes_pattern(self) -> None:
        patterns = [
            {
                "id": "shell.prompt",
                "regex": r"\$\s*$",
                "negative_regex": r"banner",
            }
        ]
        d = PromptDetector(patterns)
        snap = _snap("banner here\nuser$ ")
        diag = d.detect_prompt_with_diagnostics(snap)
        assert diag.match is None
        # The regex did fire but was rejected by the negative match.
        assert diag.regex_matched_but_failed, "expected a diagnostic entry"
        assert diag.regex_matched_but_failed[0]["reason"] == "negative_match"

    def test_negative_match_diagnostic_includes_pattern_id(self) -> None:
        patterns = [
            {"id": "p.id.literal", "regex": r"\$\s*$", "negative_regex": r"banner"}
        ]
        d = PromptDetector(patterns)
        snap = _snap("banner here\nuser$ ")
        diag = d.detect_prompt_with_diagnostics(snap)
        assert diag.regex_matched_but_failed
        assert diag.regex_matched_but_failed[0]["pattern_id"] == "p.id.literal"

    def test_negative_match_diagnostic_includes_negative_pattern_field(self) -> None:
        patterns = [
            {"id": "p", "regex": r"\$\s*$", "negative_regex": r"forbidden-substring"}
        ]
        d = PromptDetector(patterns)
        snap = _snap("forbidden-substring\nuser$ ")
        diag = d.detect_prompt_with_diagnostics(snap)
        assert diag.regex_matched_but_failed
        # The diagnostic entry must include a ``negative_pattern`` key whose value
        # is the negative regex string. Mutations that rename this key fail here.
        entry = diag.regex_matched_but_failed[0]
        assert "negative_pattern" in entry, f"missing negative_pattern in {entry!r}"
        assert "forbidden-substring" in str(entry["negative_pattern"])

    def test_negative_match_is_case_insensitive(self) -> None:
        """Comment in source says negative_match runs with re.IGNORECASE."""
        patterns = [{"id": "p", "regex": r"\$\s*$", "negative_regex": r"stardock"}]
        d = PromptDetector(patterns)
        # Mixed case "STARDOCK" must still trigger the negative match.
        snap = _snap("STARDOCK\nuser$ ")
        diag = d.detect_prompt_with_diagnostics(snap)
        assert diag.match is None, "negative match should be case-insensitive"
        assert diag.regex_matched_but_failed
        assert diag.regex_matched_but_failed[0]["reason"] == "negative_match"


# ---------------------------------------------------------------------------
# Cursor-position check observable in diagnostics
# ---------------------------------------------------------------------------


class TestDetectInTextCursorPosition:
    """``expect_cursor_at_end`` mismatch records ``reason=cursor_position``."""

    # Multi-line screen so the cursor at y=0 falls OUTSIDE the prompt region
    # (last line is the prompt), which forces the second-pass full-screen scan
    # where cursor-at-end-requiring patterns are still tried.
    _SCREEN_MULTI = "header line\n\n\n\n\n\n\n\n\n\n\n\n\nuser$ "

    def test_cursor_not_at_end_skips_pattern(self) -> None:
        patterns = [
            {"id": "p", "regex": r"\$\s*$", "expect_cursor_at_end": True}
        ]
        d = PromptDetector(patterns)
        snap = _snap(self._SCREEN_MULTI, cursor_at_end=False, cursor_y=0)
        diag = d.detect_prompt_with_diagnostics(snap)
        assert diag.match is None
        assert diag.regex_matched_but_failed
        assert diag.regex_matched_but_failed[0]["reason"] == "cursor_position"

    def test_cursor_position_diagnostic_uses_pattern_id_key(self) -> None:
        """Diagnostic entry uses literal ``pattern_id`` key (not 'XXpattern_idXX')."""
        patterns = [
            {"id": "shell.x", "regex": r"\$\s*$", "expect_cursor_at_end": True}
        ]
        d = PromptDetector(patterns)
        snap = _snap(self._SCREEN_MULTI, cursor_at_end=False, cursor_y=0)
        diag = d.detect_prompt_with_diagnostics(snap)
        assert diag.regex_matched_but_failed
        entry = diag.regex_matched_but_failed[0]
        assert "pattern_id" in entry
        assert entry["pattern_id"] == "shell.x"

    def test_cursor_position_diagnostic_reports_expected_and_actual(self) -> None:
        patterns = [
            {"id": "p", "regex": r"\$\s*$", "expect_cursor_at_end": True}
        ]
        d = PromptDetector(patterns)
        snap = _snap(self._SCREEN_MULTI, cursor_at_end=False, cursor_y=0)
        diag = d.detect_prompt_with_diagnostics(snap)
        entry = diag.regex_matched_but_failed[0]
        assert entry["expected_cursor_at_end"] is True
        assert entry["actual_cursor_at_end"] is False

    def test_cursor_at_end_true_allows_match(self) -> None:
        patterns = [
            {"id": "p", "regex": r"\$\s*$", "expect_cursor_at_end": True}
        ]
        d = PromptDetector(patterns)
        snap = _snap("user$ ", cursor_at_end=True)
        diag = d.detect_prompt_with_diagnostics(snap)
        assert diag.match is not None
        assert diag.match.prompt_id == "p"


# ---------------------------------------------------------------------------
# Successful match shape
# ---------------------------------------------------------------------------


class TestDetectInTextMatchShape:
    """A successful match returns the full PromptMatch with defaults."""

    def test_input_type_defaults_to_multi_key(self) -> None:
        patterns = [{"id": "p", "regex": r"\$\s*$"}]
        d = PromptDetector(patterns)
        snap = _snap("user$ ")
        diag = d.detect_prompt_with_diagnostics(snap)
        assert diag.match is not None
        assert diag.match.input_type == "multi_key"

    def test_input_type_uses_pattern_value_when_set(self) -> None:
        patterns = [{"id": "p", "regex": r"\$\s*$", "input_type": "any_key"}]
        d = PromptDetector(patterns)
        snap = _snap("user$ ")
        diag = d.detect_prompt_with_diagnostics(snap)
        assert diag.match is not None
        assert diag.match.input_type == "any_key"

    def test_eol_pattern_defaults_to_crlf(self) -> None:
        patterns = [{"id": "p", "regex": r"\$\s*$"}]
        d = PromptDetector(patterns)
        snap = _snap("user$ ")
        diag = d.detect_prompt_with_diagnostics(snap)
        assert diag.match is not None
        assert diag.match.eol_pattern == r"[\r\n]+"

    def test_eol_pattern_uses_pattern_value_when_set(self) -> None:
        patterns = [{"id": "p", "regex": r"\$\s*$", "eol_pattern": r"\r"}]
        d = PromptDetector(patterns)
        snap = _snap("user$ ")
        diag = d.detect_prompt_with_diagnostics(snap)
        assert diag.match is not None
        assert diag.match.eol_pattern == r"\r"

    def test_kv_extract_threaded_through(self) -> None:
        kv = {"key1": r"(?P<v>\w+)"}
        patterns = [{"id": "p", "regex": r"\$\s*$", "kv_extract": kv}]
        d = PromptDetector(patterns)
        snap = _snap("user$ ")
        diag = d.detect_prompt_with_diagnostics(snap)
        assert diag.match is not None
        assert diag.match.kv_extract == kv

    def test_match_returns_pattern_with_same_contents(self) -> None:
        original = {"id": "p", "regex": r"\$\s*$"}
        d = PromptDetector([original])
        snap = _snap("user$ ")
        diag = d.detect_prompt_with_diagnostics(snap)
        assert diag.match is not None
        # PromptMatch is a pydantic model — the ``pattern`` field is value-equal
        # to the input even if the dict is copied during validation.
        assert diag.match.pattern == original


# ---------------------------------------------------------------------------
# First-match wins (loop continues only on no-match / negative / cursor-skip)
# ---------------------------------------------------------------------------


class TestDetectInTextFirstWins:
    def test_first_matching_pattern_wins_when_both_match(self) -> None:
        patterns = [
            {"id": "first", "regex": r"\$\s*$"},
            {"id": "second", "regex": r"\$\s*$"},
        ]
        d = PromptDetector(patterns)
        snap = _snap("user$ ")
        diag = d.detect_prompt_with_diagnostics(snap)
        assert diag.match is not None
        assert diag.match.prompt_id == "first"

    def test_second_pattern_wins_when_first_skipped_by_negative(self) -> None:
        patterns = [
            {"id": "first", "regex": r"\$\s*$", "negative_regex": r"banner"},
            {"id": "second", "regex": r"\$\s*$"},
        ]
        d = PromptDetector(patterns)
        snap = _snap("banner\nuser$ ")
        diag = d.detect_prompt_with_diagnostics(snap)
        assert diag.match is not None
        assert diag.match.prompt_id == "second"
