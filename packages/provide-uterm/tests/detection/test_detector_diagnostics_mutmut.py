#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Surgical mutmut-killer tests for ``detect_prompt_with_diagnostics`` and
``prompt_fingerprint`` on ``PromptDetector``.

Targets the logger calls, cursor-fallback branch, fingerprint composition,
and the fingerprint's sensitivity to each individual snapshot field.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from provide.uterm.detection.detector import PromptDetector


def _snap(
    screen: str,
    *,
    cursor_at_end: bool = True,
    has_trailing_space: bool = False,
    cursor_x: int = 0,
    cursor_y: int = 0,
) -> dict[str, Any]:
    return {
        "screen": screen,
        "cursor_at_end": cursor_at_end,
        "has_trailing_space": has_trailing_space,
        "cursor": {"x": cursor_x, "y": cursor_y},
    }


# ---------------------------------------------------------------------------
# detect_prompt_with_diagnostics — logger calls
# ---------------------------------------------------------------------------


class TestDiagnosticsLogging:
    def test_detection_start_log_includes_pattern_count(self, caplog: pytest.LogCaptureFixture) -> None:
        patterns = [{"id": f"p{i}", "regex": f"x{i}"} for i in range(5)]
        d = PromptDetector(patterns)
        with caplog.at_level(logging.DEBUG, logger="provide.uterm.detection.detector"):
            d.detect_prompt_with_diagnostics(_snap("nothing"))
        starts = [r for r in caplog.records if "prompt_detection_start" in r.getMessage()]
        assert starts
        assert "pattern_count=5" in starts[0].getMessage()

    def test_cursor_log_includes_both_flags(self, caplog: pytest.LogCaptureFixture) -> None:
        d = PromptDetector([{"id": "p", "regex": "x"}])
        with caplog.at_level(logging.DEBUG, logger="provide.uterm.detection.detector"):
            d.detect_prompt_with_diagnostics(_snap("y", cursor_at_end=False, has_trailing_space=True))
        cursor_logs = [r for r in caplog.records if "prompt_detection_cursor" in r.getMessage()]
        assert cursor_logs
        msg = cursor_logs[0].getMessage()
        assert "cursor_at_end=False" in msg
        assert "has_trailing_space=True" in msg

    def test_match_found_logs_prompt_id(self, caplog: pytest.LogCaptureFixture) -> None:
        d = PromptDetector([{"id": "shell.prompt", "regex": r"\$\s*$"}])
        with caplog.at_level(logging.INFO, logger="provide.uterm.detection.detector"):
            diag = d.detect_prompt_with_diagnostics(_snap("user$ "))
        assert diag.match is not None
        match_logs = [r for r in caplog.records if "prompt_detection_matched" in r.getMessage()]
        assert match_logs
        assert "shell.prompt" in match_logs[0].getMessage()

    def test_failed_partial_log_lists_failures(self, caplog: pytest.LogCaptureFixture) -> None:
        """When all patterns are partial-misses, an ERROR log lists them."""
        # Negative-match rejects the only positive pattern; no overall match.
        patterns = [{"id": "p", "regex": r"\$\s*$", "negative_regex": r"banner"}]
        d = PromptDetector(patterns)
        with caplog.at_level(logging.ERROR, logger="provide.uterm.detection.detector"):
            d.detect_prompt_with_diagnostics(_snap("banner\nuser$ "))
        failed_logs = [r for r in caplog.records if "prompt_detection_failed" in r.getMessage()]
        assert failed_logs


# ---------------------------------------------------------------------------
# detect_prompt_with_diagnostics — cursor-fallback branch
# ---------------------------------------------------------------------------


class TestDiagnosticsCursorFallback:
    """The fallback fires only when all three conditions are true:
    cursor_miss_candidates non-empty, cursor_at_end == False, has_trailing_space == True.
    """

    _SCREEN = "header\n\n\n\n\n\n\n\n\n\n\n\n\nuser$ "

    def test_fallback_uses_cursor_miss_when_all_three_conditions_true(self) -> None:
        patterns = [{"id": "p", "regex": r"\$\s*$", "expect_cursor_at_end": True}]
        d = PromptDetector(patterns)
        diag = d.detect_prompt_with_diagnostics(
            _snap(self._SCREEN, cursor_at_end=False, has_trailing_space=True, cursor_y=0)
        )
        assert diag.match is not None
        assert diag.match.prompt_id == "p"

    def test_fallback_not_used_when_cursor_at_end_true(self) -> None:
        """If cursor_at_end is True, the cursor_miss branch never fires."""
        patterns = [{"id": "p", "regex": r"\$\s*$", "expect_cursor_at_end": True}]
        d = PromptDetector(patterns)
        # With cursor_at_end=True, the main detection succeeds — match is returned
        # from the primary path, not from the fallback.
        diag = d.detect_prompt_with_diagnostics(
            _snap(self._SCREEN, cursor_at_end=True, has_trailing_space=True, cursor_y=0)
        )
        assert diag.match is not None

    def test_fallback_not_used_when_has_trailing_space_false(self) -> None:
        patterns = [{"id": "p", "regex": r"\$\s*$", "expect_cursor_at_end": True}]
        d = PromptDetector(patterns)
        diag = d.detect_prompt_with_diagnostics(
            _snap(self._SCREEN, cursor_at_end=False, has_trailing_space=False, cursor_y=0)
        )
        # No match because the gating "has_trailing_space" is False.
        assert diag.match is None
        # Diagnostic still records the cursor_position partial.
        assert diag.regex_matched_but_failed
        assert diag.regex_matched_but_failed[0]["reason"] == "cursor_position"


# ---------------------------------------------------------------------------
# prompt_fingerprint — composition + sensitivity
# ---------------------------------------------------------------------------


class TestPromptFingerprint:
    def test_same_snapshot_gives_same_fingerprint(self) -> None:
        d = PromptDetector([])
        snap = _snap("hello world")
        assert d.prompt_fingerprint(snap) == d.prompt_fingerprint(snap)

    def test_different_screen_gives_different_fingerprint(self) -> None:
        d = PromptDetector([])
        assert d.prompt_fingerprint(_snap("a")) != d.prompt_fingerprint(_snap("b"))

    def test_cursor_at_end_flag_changes_fingerprint(self) -> None:
        d = PromptDetector([])
        fp_true = d.prompt_fingerprint(_snap("x", cursor_at_end=True))
        fp_false = d.prompt_fingerprint(_snap("x", cursor_at_end=False))
        assert fp_true != fp_false

    def test_trailing_space_flag_changes_fingerprint(self) -> None:
        d = PromptDetector([])
        fp_t = d.prompt_fingerprint(_snap("x", has_trailing_space=True))
        fp_f = d.prompt_fingerprint(_snap("x", has_trailing_space=False))
        assert fp_t != fp_f

    def test_cursor_x_position_changes_fingerprint(self) -> None:
        d = PromptDetector([])
        fp_x0 = d.prompt_fingerprint(_snap("x", cursor_x=0))
        fp_x5 = d.prompt_fingerprint(_snap("x", cursor_x=5))
        assert fp_x0 != fp_x5

    def test_cursor_y_position_changes_fingerprint(self) -> None:
        d = PromptDetector([])
        fp_y0 = d.prompt_fingerprint(_snap("x", cursor_y=0))
        fp_y5 = d.prompt_fingerprint(_snap("x", cursor_y=5))
        assert fp_y0 != fp_y5

    def test_fingerprint_format_is_h_colon_int_colon_int_colon_int_colon_int(self) -> None:
        """Fingerprint format: ``{blake2s_hex}:{at_end}:{trailing}:{cx}:{cy}``."""
        d = PromptDetector([])
        fp = d.prompt_fingerprint(_snap("hi", cursor_at_end=True, has_trailing_space=False, cursor_x=3, cursor_y=2))
        parts = fp.split(":")
        assert len(parts) == 5, f"unexpected fingerprint shape: {fp!r}"
        assert parts[1] == "1", "cursor_at_end True → '1'"
        assert parts[2] == "0", "trailing False → '0'"
        assert parts[3] == "3", "cursor_x position"
        assert parts[4] == "2", "cursor_y position"

    def test_fingerprint_blake2s_hex_first_segment(self) -> None:
        """First colon-separated segment is a hex blake2s digest (64 chars)."""
        d = PromptDetector([])
        fp = d.prompt_fingerprint(_snap("hello"))
        h = fp.split(":")[0]
        assert len(h) == 64, f"blake2s hex digest should be 64 chars; got {len(h)}"
        assert all(c in "0123456789abcdef" for c in h), f"non-hex chars in {h!r}"

    def test_empty_screen_yields_stable_fingerprint(self) -> None:
        d = PromptDetector([])
        fp1 = d.prompt_fingerprint(_snap(""))
        fp2 = d.prompt_fingerprint(_snap(""))
        assert fp1 == fp2

    def test_normalizer_affects_fingerprint(self) -> None:
        """Passing a normalizer that strips digits should fold ``a1`` and ``a2`` together."""
        import re as _re

        def strip_digits(s: str) -> str:
            return _re.sub(r"\d", "", s)

        d = PromptDetector([], normalizer=strip_digits)
        fp_a1 = d.prompt_fingerprint(_snap("a1"))
        fp_a2 = d.prompt_fingerprint(_snap("a2"))
        # Both normalize to "a" → same blake2s hash; cursor/flags also match.
        assert fp_a1 == fp_a2

    def test_invalid_cursor_coordinates_default_to_zero(self) -> None:
        """Cursor dict with non-int values must not crash; fingerprint should compute."""
        d = PromptDetector([])
        snap: dict[str, Any] = {
            "screen": "hi",
            "cursor_at_end": True,
            "has_trailing_space": False,
            "cursor": {"x": "not-an-int", "y": object()},
        }
        fp = d.prompt_fingerprint(snap)
        # The defensive except branch falls back to (0, 0).
        assert fp.endswith(":0:0")

    def test_missing_cursor_at_end_defaults_to_true(self) -> None:
        """``cursor_at_end`` defaults to True when absent from snapshot."""
        d = PromptDetector([])
        snap = {"screen": "x", "cursor": {"x": 0, "y": 0}}
        fp = d.prompt_fingerprint(snap)
        # cursor_at_end defaults to True (1), trailing defaults to False (0)
        parts = fp.split(":")
        assert parts[1] == "1"


# ---------------------------------------------------------------------------
# add_pattern / reload_patterns — recompile + filter consistency
# ---------------------------------------------------------------------------


class TestAddReloadPatterns:
    def test_add_pattern_increases_compiled_count(self) -> None:
        d = PromptDetector([{"id": "p1", "regex": "a"}])
        before = len(d._compiled_all)
        d.add_pattern({"id": "p2", "regex": "b"})
        assert len(d._compiled_all) == before + 1

    def test_add_pattern_appears_in_compiled_list(self) -> None:
        d = PromptDetector([{"id": "p1", "regex": "a"}])
        d.add_pattern({"id": "p2.new", "regex": "b"})
        ids = [p["id"] for (_re, p) in d._compiled_all]
        assert "p2.new" in ids

    def test_add_pattern_with_expect_cursor_false_appears_in_no_cursor_list(self) -> None:
        d = PromptDetector([{"id": "p1", "regex": "a"}])
        before = len(d._compiled_no_cursor_end_req)
        d.add_pattern({"id": "p2", "regex": "b", "expect_cursor_at_end": False})
        assert len(d._compiled_no_cursor_end_req) == before + 1

    def test_add_pattern_with_expect_cursor_true_not_in_no_cursor_list(self) -> None:
        d = PromptDetector([])
        d.add_pattern({"id": "p", "regex": "b", "expect_cursor_at_end": True})
        assert d._compiled_no_cursor_end_req == []

    def test_reload_patterns_replaces_existing(self) -> None:
        d = PromptDetector([{"id": "old", "regex": "x"}])
        d.reload_patterns([{"id": "new1", "regex": "y"}, {"id": "new2", "regex": "z"}])
        ids = [p["id"] for (_re, p) in d._compiled_all]
        assert ids == ["new1", "new2"]

    def test_reload_patterns_resets_no_cursor_list(self) -> None:
        d = PromptDetector([{"id": "old", "regex": "x", "expect_cursor_at_end": False}])
        assert len(d._compiled_no_cursor_end_req) == 1
        d.reload_patterns([{"id": "new", "regex": "y", "expect_cursor_at_end": True}])
        assert d._compiled_no_cursor_end_req == []

    def test_reload_patterns_empty_clears_everything(self) -> None:
        d = PromptDetector([{"id": "p1", "regex": "x"}, {"id": "p2", "regex": "y"}])
        d.reload_patterns([])
        assert d._compiled_all == []
        assert d._compiled_no_cursor_end_req == []


# ---------------------------------------------------------------------------
# __init__ filter cluster
# ---------------------------------------------------------------------------


class TestInitFilters:
    def test_compiled_no_cursor_end_req_excludes_expect_cursor_true(self) -> None:
        d = PromptDetector(
            [
                {"id": "yes", "regex": "x", "expect_cursor_at_end": True},
                {"id": "no", "regex": "y", "expect_cursor_at_end": False},
            ]
        )
        ids = [p["id"] for (_re, p) in d._compiled_no_cursor_end_req]
        assert ids == ["no"]

    def test_compiled_no_cursor_end_req_treats_missing_as_true(self) -> None:
        """Missing ``expect_cursor_at_end`` defaults to True, so excluded."""
        d = PromptDetector([{"id": "p", "regex": "x"}])  # no expect_cursor_at_end
        assert d._compiled_no_cursor_end_req == []

    def test_init_stores_normalizer_reference(self) -> None:
        def norm(s: str) -> str:
            return s.upper()

        d = PromptDetector([], normalizer=norm)
        assert d._normalizer is norm

    def test_init_default_normalizer_is_none(self) -> None:
        d = PromptDetector([])
        assert d._normalizer is None

    def test_init_stores_patterns_reference(self) -> None:
        ps = [{"id": "p", "regex": "x"}]
        d = PromptDetector(ps)
        assert d._patterns is ps

    def test_init_pattern_count_property(self) -> None:
        d = PromptDetector([{"id": f"p{i}", "regex": f"x{i}"} for i in range(7)])
        assert d.pattern_count == 7

    def test_init_compiled_legacy_alias_matches_compiled_all(self) -> None:
        """Legacy ``_compiled`` is aliased to ``_compiled_all``."""
        d = PromptDetector([{"id": "p", "regex": "x"}])
        assert d._compiled is d._compiled_all
