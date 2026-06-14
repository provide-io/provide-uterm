#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Logging, reload, and atomicity survivor tests for detector.py."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from provide.uterm.detection.detector import (
    DetectorPatternCompileError,
    PromptDetector,
)


class TestDiagnosticsRegionLogPayload:
    """The structured-log payload for the region-debug log path.

    Mutants dpwd_46/47/112 substitute log args with None — observable via
    caplog format-failure or rendered message content.
    """

    def test_region_log_records_cursor_in_region_and_tail(self, caplog: pytest.LogCaptureFixture) -> None:
        """Targets dpwd_46 (cursor_in_region → None) and dpwd_47
        (region_text[-200:] → None).

        Under the original, the rendered message contains a sensible
        value for cursor_in_region (True/False as string) and a region
        tail string. Under mutant 46, the message contains "None" where
        the cursor flag would be. Under mutant 47, the tail is "None".
        """
        d = PromptDetector([{"id": "p", "regex": r"will-not-match"}])
        screen = "hello-world-this-is-a-screen-tail"
        with caplog.at_level(logging.DEBUG, logger="provide.uterm.detection.detector"):
            d.detect_prompt_with_diagnostics({"screen": screen, "cursor": {"x": 0, "y": 0}, "cursor_at_end": True})
        region_logs = [r for r in caplog.records if "prompt_detection_region" in r.getMessage()]
        assert region_logs
        msg = region_logs[0].getMessage()
        # cursor_in_region segment must be True/False (real screen has
        # cursor at y=0, last_idx=0 → cursor_in_region=True).
        assert "cursor_in_region=True" in msg
        # region_tail segment must contain real content from the screen,
        # not the literal "None".
        assert "region_tail=hello" in msg

    def test_no_match_log_records_screen_preview(self, caplog: pytest.LogCaptureFixture) -> None:
        """Targets dpwd_112 — ``screen[-150:]`` becomes None."""
        d = PromptDetector([{"id": "p", "regex": r"will-not-match"}])
        screen = "screen-content-end"
        with caplog.at_level(logging.DEBUG, logger="provide.uterm.detection.detector"):
            d.detect_prompt_with_diagnostics({"screen": screen, "cursor": {"x": 0, "y": 0}, "cursor_at_end": True})
        no_match_logs = [r for r in caplog.records if "prompt_detection_no_match" in r.getMessage()]
        assert no_match_logs
        msg = no_match_logs[0].getMessage()
        # screen_preview segment is the real screen tail, not "None".
        assert "screen_preview=screen-content-end" in msg


class TestRunTwoPassMatchLogPayload:
    """Log-arg mutations in the match-success paths of _run_two_pass_detection."""

    def test_matched_region_log_uses_input_type(self, caplog: pytest.LogCaptureFixture) -> None:
        """Targets _run_two_pass_detection mutant 19 — input_type → None
        in the matched-region log."""
        d = PromptDetector([{"id": "myprompt", "regex": r"user", "input_type": "my_special_type"}])
        with caplog.at_level(logging.INFO, logger="provide.uterm.detection.detector"):
            d.detect_prompt_with_diagnostics({"screen": "user", "cursor": {"x": 0, "y": 0}, "cursor_at_end": True})
        logs = [r for r in caplog.records if "prompt_detection_matched_region" in r.getMessage()]
        assert logs
        msg = logs[0].getMessage()
        assert "input_type=my_special_type" in msg
        assert "prompt_id=myprompt" in msg

    def test_matched_full_log_uses_prompt_id_and_input_type(self, caplog: pytest.LogCaptureFixture) -> None:
        """Targets rtpd_40 (prompt_id → None) and rtpd_41 (input_type → None)
        in the matched-full log.

        Force a full-screen-only match: regex matches at a line that
        falls OUTSIDE the tail region (tail_lines=12) so the region
        pass returns no match, then the full-screen pass matches.
        Cursor at y=0 → cursor_in_region=False → full-screen pass runs.
        """
        # Put the unique target at line 0, then enough tail content so the
        # region (last 12 lines) doesn't contain "uniquetoken".
        screen = "uniquetoken\n" + "\n".join(f"line{i}" for i in range(30))
        d = PromptDetector(
            [
                {
                    "id": "fullprompt",
                    "regex": r"uniquetoken",
                    "input_type": "fullkind",
                    "expect_cursor_at_end": True,
                }
            ]
        )
        with caplog.at_level(logging.INFO, logger="provide.uterm.detection.detector"):
            d.detect_prompt_with_diagnostics({"screen": screen, "cursor": {"x": 0, "y": 0}, "cursor_at_end": True})
        logs = [r for r in caplog.records if "prompt_detection_matched_full" in r.getMessage()]
        assert logs
        msg = logs[0].getMessage()
        assert "prompt_id=fullprompt" in msg
        assert "input_type=fullkind" in msg


class TestFallbackLogPayload:
    """The cursor-heuristic-fallback warning log payload."""

    def test_fallback_log_uses_cand_prompt_id(self, caplog: pytest.LogCaptureFixture) -> None:
        """Targets dpwd_85 — ``cand.prompt_id`` → None in the fallback log."""
        d = PromptDetector([{"id": "fallbackid", "regex": r"user", "expect_cursor_at_end": True}])
        # Long screen → full-screen pass → cursor_miss candidate recorded.
        screen = "\n".join(f"line{i}" for i in range(30)) + "\nuser"
        with caplog.at_level(logging.WARNING, logger="provide.uterm.detection.detector"):
            d.detect_prompt_with_diagnostics(
                {
                    "screen": screen,
                    "cursor_at_end": False,
                    "has_trailing_space": True,
                    "cursor": {"x": 0, "y": 0},
                }
            )
        logs = [r for r in caplog.records if "prompt_detection_cursor_heuristic_fallback" in r.getMessage()]
        assert logs
        msg = logs[0].getMessage()
        # Under original: "...fallback_prompt_id=fallbackid".
        # Under mutant: "...fallback_prompt_id=None".
        assert "fallback_prompt_id=fallbackid" in msg


class TestNoMatchLogFailuresPayload:
    """The no-positive-match ERROR log's failures list."""

    def test_failures_list_includes_pattern_id_and_reason(self, caplog: pytest.LogCaptureFixture) -> None:
        """Targets dpwd_96 — drops the whole failures list (arg → None).

        Under original, the rendered log contains the partial-match
        pattern ids. Under mutant 96, the arg is None → "None" appears
        instead.
        """
        d = PromptDetector([{"id": "partial", "regex": r"user", "expect_cursor_at_end": True}])
        # Long screen → full-screen pass evaluates the strict pattern →
        # cursor_position diagnostic recorded. has_trailing_space=False so
        # the fallback doesn't fire → the no-match ERROR log emits.
        screen = "\n".join(f"line{i}" for i in range(30)) + "\nuser"
        with caplog.at_level(logging.ERROR, logger="provide.uterm.detection.detector"):
            d.detect_prompt_with_diagnostics(
                {
                    "screen": screen,
                    "cursor_at_end": False,
                    "has_trailing_space": False,
                    "cursor": {"x": 0, "y": 0},
                }
            )
        logs = [r for r in caplog.records if "prompt_detection_failed" in r.getMessage()]
        assert logs
        msg = logs[0].getMessage()
        # The real partial-match pattern_id appears in the rendered list.
        assert "partial" in msg
        # And the reason string.
        assert "cursor_position" in msg
        # The rendered dict uses the lowercase keys "pattern_id" and "reason"
        # — kills key-rename mutants 102/103/106/107.
        assert "'pattern_id'" in msg
        assert "'reason'" in msg
        # Mutants would render "'XXpattern_idXX'" or "'PATTERN_ID'" etc.
        assert "XX" not in msg
        assert "PATTERN_ID" not in msg
        assert "REASON" not in msg


# ---------------------------------------------------------------------------
# reload_patterns — the _compiled_no_cursor_end_req filter
# ---------------------------------------------------------------------------


class TestReloadPatternsFilter:
    """``reload_patterns`` rebuilds ``_compiled_no_cursor_end_req``.

    The filter ``not bool(pat.get("expect_cursor_at_end", True))`` is
    observable via the size of ``_compiled_no_cursor_end_req`` after
    reload — patterns with ``expect_cursor_at_end=False`` end up in the
    list; the rest don't.
    """

    def _patterns_with_one_no_cursor(self) -> list[dict[str, Any]]:
        return [
            {"id": "needs_cursor", "regex": r"a", "expect_cursor_at_end": True},
            {"id": "lenient", "regex": r"b", "expect_cursor_at_end": False},
            {"id": "default_required", "regex": r"c"},  # default True
        ]

    def test_reload_filter_uses_expect_cursor_at_end_key(self) -> None:
        """Targets reload_patterns mutants 6 (key=None), 9 (drop kwarg).

        Both mutants make ``pat.get`` always return None → ``not bool(None)``
        is True → ALL patterns end up in the filtered list.
        """
        d = PromptDetector([{"id": "x", "regex": r"x"}])
        d.reload_patterns(self._patterns_with_one_no_cursor())
        # Original: only "lenient" lands in the no-cursor list (1 entry).
        # Mutant 6/9: all 3 patterns land in the list.
        assert len(d._compiled_no_cursor_end_req) == 1
        assert d._compiled_no_cursor_end_req[0][1]["id"] == "lenient"

    def test_reload_filter_default_true_excludes_pattern_without_key(self) -> None:
        """Targets reload_patterns mutants 7 (default=None), 12 (default=False).

        With the original default True, a pattern lacking the key is
        EXCLUDED. With mutant defaults None/False, ``not bool(default)``
        is True → INCLUDED.
        """
        d = PromptDetector([{"id": "x", "regex": r"x"}])
        d.reload_patterns([{"id": "default_required", "regex": r"c"}])  # no key
        # Original: 0 patterns in the no-cursor list.
        # Mutants 7/12: 1 pattern (the default-True / default-False flip).
        assert len(d._compiled_no_cursor_end_req) == 0

    def test_reload_compiled_legacy_attr_equals_compiled_all(self) -> None:
        """Targets reload_patterns mutant 13 — ``self._compiled = None``."""
        d = PromptDetector([{"id": "x", "regex": r"x"}])
        d.reload_patterns([{"id": "a", "regex": r"a"}, {"id": "b", "regex": r"b"}])
        # Legacy attribute exists and points to the same list.
        assert d._compiled is not None
        assert d._compiled is d._compiled_all
        assert len(d._compiled) == 2


class TestAddPatternLegacyCompiledAttr:
    """``add_pattern`` rebuilds the legacy ``_compiled`` attr."""

    def test_add_pattern_compiled_legacy_attr_set_to_compiled_all(self) -> None:
        """Targets add_pattern mutant 13 — ``self._compiled = None``."""
        d = PromptDetector([{"id": "x", "regex": r"x"}])
        d.add_pattern({"id": "y", "regex": r"y"})
        assert d._compiled is not None
        assert d._compiled is d._compiled_all
        assert len(d._compiled) == 2


class TestPatternMutationAtomicity:
    """add_pattern / reload_patterns must be atomic (compile-then-swap).

    In strict mode a bad pattern must roll back so the detector is never
    left holding a poisoned ``_patterns`` list that re-raises forever.
    """

    def test_add_bad_pattern_strict_does_not_wedge_detector(self) -> None:
        det = PromptDetector(patterns=[{"id": "ok", "regex": "ready>"}], strict=True)
        with pytest.raises(DetectorPatternCompileError):
            det.add_pattern({"id": "bad", "regex": "("})  # invalid regex
        # State must be unchanged: the good pattern still works.
        assert det.pattern_count == 1
        # A subsequent good add must succeed (not re-raise on a poisoned list).
        det.add_pattern({"id": "ok2", "regex": "done>"})
        assert det.pattern_count == 2
        assert len(det._compiled_all) == 2

    def test_add_bad_pattern_strict_rolls_back_compiled(self) -> None:
        det = PromptDetector(patterns=[{"id": "ok", "regex": "ready>"}], strict=True)
        compiled_before = det._compiled_all
        with pytest.raises(DetectorPatternCompileError):
            det.add_pattern({"id": "bad", "regex": "("})
        # Compiled artifacts untouched by the failed add.
        assert det._compiled_all is compiled_before
        assert det._compiled is det._compiled_all
        assert len(det._compiled_all) == 1

    def test_reload_bad_pattern_strict_rolls_back(self) -> None:
        det = PromptDetector(patterns=[{"id": "ok", "regex": "ready>"}], strict=True)
        with pytest.raises(DetectorPatternCompileError):
            det.reload_patterns([{"id": "bad", "regex": "("}])
        # Original good pattern is preserved, not replaced by the bad set.
        assert det.pattern_count == 1
        assert det._patterns[0]["id"] == "ok"
        # Recovery still works.
        det.reload_patterns([{"id": "ok2", "regex": "done>"}])
        assert det.pattern_count == 1
        assert det._patterns[0]["id"] == "ok2"

    def test_add_good_pattern_strict_commits(self) -> None:
        det = PromptDetector(patterns=[{"id": "ok", "regex": "ready>"}], strict=True)
        det.add_pattern({"id": "ok2", "regex": "done>"})
        assert det.pattern_count == 2
        assert [p["id"] for p in det._patterns] == ["ok", "ok2"]
