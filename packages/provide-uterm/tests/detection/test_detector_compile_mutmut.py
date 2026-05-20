#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Surgical mutmut-killer tests for ``PromptDetector._compile_patterns``.

The default :func:`test_compile_patterns_*` tests in ``test_detector.py``
exercise success / fail paths but don't assert on the logger calls or the
exact shape of the internal ``failed_patterns`` diagnostic dict — both of
which carry a high density of mutmut mutations (logger arg reordering,
dict key renaming, sentinel-value swaps, ``re.MULTILINE`` flag drops, etc.).

Tests here observe those internals via ``caplog`` and via patching the
detector's bound ``failed_patterns`` capture point. Tests are intentionally
focused on observable consequences of each mutation rather than reading
implementation lines, so they stay valid through reasonable refactors.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from unittest.mock import patch

import pytest

from provide.uterm.detection.detector import PromptDetector

# ---------------------------------------------------------------------------
# Logger-content assertions — kill mutations that reorder or strip logger args
# ---------------------------------------------------------------------------


class TestCompilePatternsLogging:
    """Verify logger.info/debug/exception/error calls reflect real values.

    Mutations that swap ``logger.info("compile_start count=%d", n)`` for
    ``logger.info(n)`` change the formatted output, so caplog observes the
    difference even when the returned compiled list is identical.
    """

    def test_start_log_includes_count_keyword(self, caplog: pytest.LogCaptureFixture) -> None:
        patterns = [{"id": "p1", "regex": "a"}, {"id": "p2", "regex": "b"}, {"id": "p3", "regex": "c"}]
        with caplog.at_level(logging.INFO, logger="provide.uterm.detection.detector"):
            PromptDetector(patterns)
        starts = [r for r in caplog.records if "pattern_compile_start" in r.getMessage()]
        assert starts, "missing pattern_compile_start INFO log"
        assert "count=3" in starts[0].getMessage(), f"start log did not include count=3: {starts[0].getMessage()!r}"

    def test_start_log_count_matches_zero_for_empty_input(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="provide.uterm.detection.detector"):
            PromptDetector([])
        starts = [r for r in caplog.records if "pattern_compile_start" in r.getMessage()]
        assert starts and "count=0" in starts[0].getMessage()

    def test_complete_log_reports_succeeded_count(self, caplog: pytest.LogCaptureFixture) -> None:
        patterns = [
            {"id": "ok1", "regex": "a"},
            {"id": "ok2", "regex": "b"},
            {"id": "bad", "regex": "[invalid("},  # compile fails
        ]
        with caplog.at_level(logging.INFO, logger="provide.uterm.detection.detector"):
            PromptDetector(patterns)
        completes = [r for r in caplog.records if "pattern_compile_complete" in r.getMessage()]
        assert completes
        msg = completes[0].getMessage()
        assert "succeeded=2" in msg, f"expected succeeded=2 in {msg!r}"
        assert "failed=1" in msg, f"expected failed=1 in {msg!r}"

    def test_ok_log_emitted_per_compiled_pattern(self, caplog: pytest.LogCaptureFixture) -> None:
        patterns = [{"id": "pA", "regex": "x"}, {"id": "pB", "regex": "y"}]
        with caplog.at_level(logging.DEBUG, logger="provide.uterm.detection.detector"):
            PromptDetector(patterns)
        ok_logs = [r for r in caplog.records if "pattern_compile_ok" in r.getMessage()]
        assert len(ok_logs) == 2, f"expected 2 pattern_compile_ok records, got {len(ok_logs)}"
        ok_msgs = "\n".join(r.getMessage() for r in ok_logs)
        assert "pA" in ok_msgs
        assert "pB" in ok_msgs

    def test_failed_log_carries_pattern_id_and_regex(self, caplog: pytest.LogCaptureFixture) -> None:
        patterns = [{"id": "broken.x", "regex": "[unclosed"}]
        with caplog.at_level(logging.ERROR, logger="provide.uterm.detection.detector"):
            PromptDetector(patterns)
        failures = [r for r in caplog.records if "pattern_compile_failed" in r.getMessage()]
        assert failures, "expected a pattern_compile_failed log"
        msg = failures[0].getMessage()
        assert "broken.x" in msg, f"failed log did not include pattern id 'broken.x': {msg!r}"
        assert "[unclosed" in msg, f"failed log did not include the regex string: {msg!r}"

    def test_invalid_structure_log_carries_missing_key(self, caplog: pytest.LogCaptureFixture) -> None:
        patterns = [{"id": "no.regex"}]  # KeyError on pattern["regex"]
        with caplog.at_level(logging.ERROR, logger="provide.uterm.detection.detector"):
            PromptDetector(patterns)
        invalid = [r for r in caplog.records if "pattern_compile_invalid_structure" in r.getMessage()]
        assert invalid, "expected pattern_compile_invalid_structure log"
        assert "no.regex" in invalid[0].getMessage()

    def test_failures_summary_log_only_on_failures(self, caplog: pytest.LogCaptureFixture) -> None:
        """``pattern_compile_failures`` ERROR is only emitted when at least one pattern failed."""
        with caplog.at_level(logging.ERROR, logger="provide.uterm.detection.detector"):
            PromptDetector([{"id": "only.good", "regex": "x"}])
        summary = [r for r in caplog.records if "pattern_compile_failures" in r.getMessage()]
        assert not summary, "summary log fired with zero failures"

    def test_failures_summary_log_includes_failure_count(self, caplog: pytest.LogCaptureFixture) -> None:
        patterns = [{"id": "b1", "regex": "[bad"}, {"id": "b2", "regex": "(also-bad"}]
        with caplog.at_level(logging.ERROR, logger="provide.uterm.detection.detector"):
            PromptDetector(patterns)
        summary = [r for r in caplog.records if "pattern_compile_failures" in r.getMessage()]
        assert summary
        assert "count=2" in summary[0].getMessage()


# ---------------------------------------------------------------------------
# failed_patterns dict-key + sentinel-value assertions
# ---------------------------------------------------------------------------


class TestCompilePatternsFailedShape:
    """The ``failed_patterns`` list is a private diagnostic; assert its shape.

    Patch the module-level ``logger.error`` to capture the structured payload
    passed to the summary log, then inspect the dict shape directly.
    """

    @staticmethod
    def _capture_failed(patterns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        captured: list[list[dict[str, Any]]] = []
        original_error = logging.getLogger("provide.uterm.detection.detector").error

        def tap(msg: str, *args: Any, **kwargs: Any) -> None:
            # pattern_compile_failures count=N failed=[{...}, {...}]
            # args[1] is the failed-patterns list when the format string is
            # "count=%d failed=%s"
            if "pattern_compile_failures" in msg and args and len(args) >= 2 and isinstance(args[1], list):
                captured.append(args[1])
            original_error(msg, *args, **kwargs)

        with patch.object(logging.getLogger("provide.uterm.detection.detector"), "error", side_effect=tap):
            PromptDetector(patterns)
        return captured[0] if captured else []

    def test_bad_regex_entry_uses_id_key(self) -> None:
        failed = self._capture_failed([{"id": "rx.bad", "regex": "[broken"}])
        assert failed and "id" in failed[0]
        assert failed[0]["id"] == "rx.bad"

    def test_bad_regex_entry_uses_error_key(self) -> None:
        failed = self._capture_failed([{"id": "rx.bad", "regex": "[broken"}])
        assert failed and "error" in failed[0]
        # Real re.error message; just confirm it's non-empty.
        assert failed[0]["error"]

    def test_bad_regex_entry_default_id_is_unknown(self) -> None:
        """Pattern lacking an ``id`` field gets id='unknown' in the failed entry."""
        failed = self._capture_failed([{"regex": "[broken"}])  # no "id"
        assert failed
        assert failed[0]["id"] == "unknown"

    def test_missing_regex_key_default_error_describes_key(self) -> None:
        """Missing-regex-key entry surfaces the missing key in its error string."""
        failed = self._capture_failed([{"id": "n.regex"}])
        assert failed
        # The error string is f"Missing key: {e}" where e is the KeyError on 'regex'.
        assert "regex" in failed[0]["error"]


# ---------------------------------------------------------------------------
# re.MULTILINE flag is applied
# ---------------------------------------------------------------------------


class TestCompilePatternsMultiline:
    """``re.compile(..., re.MULTILINE)`` must be invoked with MULTILINE flag.

    A mutation dropping the flag silently changes ``^`` / ``$`` semantics —
    matched against multi-line input, the same regex behaves differently.
    """

    def test_multiline_flag_lets_caret_match_each_line_start(self) -> None:
        patterns = [{"id": "ml", "regex": r"^prompt>"}]
        d = PromptDetector(patterns)
        compiled = d._compiled_all
        assert len(compiled) == 1
        regex = compiled[0][0]
        # With re.MULTILINE, ^ matches at the start of each line.
        # Without it, ^ matches only at the very start of the string.
        assert regex.flags & re.MULTILINE, "compiled regex missing re.MULTILINE flag"
        text = "intro line\nprompt> waiting"
        assert regex.search(text), "MULTILINE-aware regex should match 'prompt>' on line 2"

    def test_compiled_regex_object_is_pattern_instance(self) -> None:
        d = PromptDetector([{"id": "p", "regex": r"\$"}])
        assert isinstance(d._compiled_all[0][0], re.Pattern)


# ---------------------------------------------------------------------------
# Return-value semantics
# ---------------------------------------------------------------------------


class TestCompilePatternsReturn:
    """Mutations that affect the returned list ordering / membership."""

    def test_returns_only_successfully_compiled(self) -> None:
        patterns = [
            {"id": "ok", "regex": "a"},
            {"id": "bad", "regex": "[broken"},
            {"id": "ok2", "regex": "b"},
        ]
        d = PromptDetector(patterns)
        compiled_ids = [p["id"] for (_re, p) in d._compiled_all]
        assert compiled_ids == ["ok", "ok2"]

    def test_returned_tuples_carry_original_pattern_dict(self) -> None:
        """The second tuple element must be the *same* dict reference passed in."""
        marker = {"id": "marker", "regex": "x", "extra": "carry-me"}
        d = PromptDetector([marker])
        assert d._compiled_all[0][1] is marker

    def test_empty_input_returns_empty_list(self) -> None:
        d = PromptDetector([])
        assert d._compiled_all == []

    def test_all_invalid_returns_empty_list(self) -> None:
        d = PromptDetector([{"id": "b1", "regex": "[bad"}, {"id": "b2"}])
        assert d._compiled_all == []
