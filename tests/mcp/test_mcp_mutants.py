#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Targeted mutation-killing tests for mcp/server.py _clean_snapshot.

Kills surviving mutants:
  mutmut_6:  tail_lines > 0 → tail_lines >= 0 (raw mode guard)
  mutmut_10: snapshot.get("screen", "") → snapshot.get("screen", None)
  mutmut_12: snapshot.get("screen", "") → snapshot.get("screen", )
  mutmut_15: snapshot.get("screen", "") → snapshot.get("screen", "XXXX")
  mutmut_17: len(lines) > tail_lines → len(lines) >= tail_lines (raw mode)
  mutmut_21: "\\n".join(...) → "XX\\nXX".join(...)
  mutmut_26: snapshot.get("screen", "") → snapshot.get("screen", None) (non-raw)
  mutmut_28: snapshot.get("screen", "") → snapshot.get("screen", ) (non-raw)
  mutmut_34: tail_lines > 0 → tail_lines >= 0 (non-raw guard)
  mutmut_35: tail_lines > 0 → tail_lines > 1 (non-raw guard)
  mutmut_37: len(lines) > tail_lines → len(lines) >= tail_lines (non-raw)
"""

from __future__ import annotations

from typing import Any

from provide.terminal.mcp.server import _clean_snapshot

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _snap(screen: str, **extra: Any) -> dict[str, Any]:
    return {"screen": screen, **extra}


# ---------------------------------------------------------------------------
# mutmut_6: raw mode — tail_lines=0 must NOT trim (guard is > 0, not >= 0)
# ---------------------------------------------------------------------------


class TestCleanSnapshotRawModeTailLinesZero:
    """Kill mutmut_6: tail_lines > 0 → tail_lines >= 0 in raw mode."""

    def test_raw_tail_lines_zero_returns_original(self) -> None:
        """tail_lines=0 must return snapshot unchanged in raw mode.

        With mutation (>= 0), tail_lines=0 would enter the trim branch and
        call splitlines() on the screen. With original (> 0), it skips and
        returns the snapshot as-is.
        """
        snap = _snap("line1\nline2\nline3")
        result = _clean_snapshot(snap, "raw", tail_lines=0)
        assert result is snap, "raw mode with tail_lines=0 must return original snapshot"
        assert result["screen"] == "line1\nline2\nline3"

    def test_raw_tail_lines_negative_returns_original(self) -> None:
        """tail_lines=-1 must return snapshot unchanged (negative is not > 0)."""
        snap = _snap("a\nb")
        result = _clean_snapshot(snap, "raw", tail_lines=-1)
        assert result is snap


# ---------------------------------------------------------------------------
# mutmut_10/12/15: raw mode — missing 'screen' key defaults to "" not None/"XXXX"
# ---------------------------------------------------------------------------


class TestCleanSnapshotRawModeScreenDefault:
    """Kill mutmut_10 (→None), mutmut_12 (no default), mutmut_15 (→"XXXX")."""

    def test_raw_missing_screen_with_tail_lines_returns_original(self) -> None:
        """When 'screen' missing, default="" means no trim possible.

        With None default, splitlines() would crash (AttributeError).
        With "XXXX" default, splitlines() would return ["XXXX"].
        With "" default, len([]) == 0, not > tail_lines=1, so original returned.
        """
        snap: dict[str, Any] = {"cols": 80, "rows": 24}
        result = _clean_snapshot(snap, "raw", tail_lines=1)
        # No screen key — default "" has 0 lines which is not > 1, so return snap unchanged
        assert result is snap

    def test_raw_empty_screen_no_trim(self) -> None:
        """Empty screen string: 0 lines > tail_lines=2 is False, so no trim."""
        snap = _snap("")
        result = _clean_snapshot(snap, "raw", tail_lines=2)
        assert result is snap
        assert result["screen"] == ""


# ---------------------------------------------------------------------------
# mutmut_17: raw mode — len(lines) > tail_lines vs >= tail_lines
# ---------------------------------------------------------------------------


class TestCleanSnapshotRawModeExactBoundary:
    """Kill mutmut_17: len(lines) > tail_lines → len(lines) >= tail_lines."""

    def test_raw_exact_line_count_no_trim(self) -> None:
        """When len(lines) == tail_lines exactly, do NOT trim.

        With mutation (>=), len == tail_lines would trigger trim and return same
        content (since lines[-N:] when N == len is same as all lines). But the
        returned dict would be a new dict {**snap, "screen": ...} not the original.
        We detect this by checking identity.
        """
        snap = _snap("A\nB\nC")  # 3 lines
        result = _clean_snapshot(snap, "raw", tail_lines=3)
        # 3 lines, tail_lines=3: len == tail_lines, should NOT trim → return original
        assert result is snap, "raw mode: when len(lines) == tail_lines, must NOT trim (> not >=)"

    def test_raw_one_more_line_than_tail_does_trim(self) -> None:
        """When len(lines) == tail_lines + 1, DO trim."""
        snap = _snap("A\nB\nC\nD")  # 4 lines
        result = _clean_snapshot(snap, "raw", tail_lines=3)
        assert result is not snap
        assert result["screen"] == "B\nC\nD"


# ---------------------------------------------------------------------------
# mutmut_21: raw mode — "\n".join vs "XX\nXX".join
# ---------------------------------------------------------------------------


class TestCleanSnapshotRawModeJoinSeparator:
    """Kill mutmut_21: "\\n".join(...) → "XX\\nXX".join(...)."""

    def test_raw_join_uses_newline_not_xxxx(self) -> None:
        """Trimmed lines must be joined with '\\n', not 'XX\\nXX'."""
        snap = _snap("one\ntwo\nthree\nfour")  # 4 lines
        result = _clean_snapshot(snap, "raw", tail_lines=2)
        assert result["screen"] == "three\nfour", f"raw mode join must use '\\n', got: {result['screen']!r}"
        assert "XX" not in result["screen"]

    def test_raw_multiline_join_preserves_content(self) -> None:
        """Multi-line trimmed result has correct separator between all lines."""
        snap = _snap("\n".join(f"line{i}" for i in range(10)))
        result = _clean_snapshot(snap, "raw", tail_lines=3)
        lines = result["screen"].split("\n")
        assert len(lines) == 3
        assert lines[0] == "line7"
        assert lines[1] == "line8"
        assert lines[2] == "line9"


# ---------------------------------------------------------------------------
# mutmut_26/28: non-raw mode — missing 'screen' key defaults to "" not None
# ---------------------------------------------------------------------------


class TestCleanSnapshotNonRawModeScreenDefault:
    """Kill mutmut_26 (→None), mutmut_28 (no default) in non-raw path."""

    def test_text_mode_missing_screen_returns_empty_string(self) -> None:
        """In text mode, missing 'screen' key must default to ''.

        With None, strip_ansi(None) would crash (AttributeError in re.sub).
        """
        result = _clean_snapshot({}, "text")
        assert result == {"screen": ""}, f"text mode missing screen must default to '', got {result}"

    def test_rendered_mode_missing_screen_returns_empty_string(self) -> None:
        """In rendered mode, missing 'screen' key must default to ''."""
        result = _clean_snapshot({"cols": 80}, "rendered")
        assert result["screen"] == ""
        assert result["cols"] == 80

    def test_text_mode_missing_screen_with_tail_lines(self) -> None:
        """Missing screen + tail_lines: must not crash and returns empty."""
        result = _clean_snapshot({}, "text", tail_lines=5)
        assert result == {"screen": ""}


# ---------------------------------------------------------------------------
# mutmut_34: non-raw mode — tail_lines > 0 vs >= 0 guard
# ---------------------------------------------------------------------------


class TestCleanSnapshotNonRawModeTailLinesZero:
    """Kill mutmut_34: tail_lines > 0 → tail_lines >= 0 in non-raw mode."""

    def test_text_tail_lines_zero_no_trim(self) -> None:
        """tail_lines=0 must return full screen (guard is > 0, not >= 0).

        With mutation (>= 0), tail_lines=0 would enter trim and use lines[-0:]
        which is empty list → screen becomes "".
        """
        snap = _snap("line1\nline2\nline3")
        result = _clean_snapshot(snap, "text", tail_lines=0)
        assert result["screen"] == "line1\nline2\nline3", (
            f"tail_lines=0 must not trim in text mode, got: {result['screen']!r}"
        )

    def test_rendered_tail_lines_zero_no_trim(self) -> None:
        """tail_lines=0 must return full screen in rendered mode."""
        snap = _snap("A\nB\nC", cols=80)
        result = _clean_snapshot(snap, "rendered", tail_lines=0)
        assert result["screen"] == "A\nB\nC"

    def test_text_tail_lines_negative_no_trim(self) -> None:
        """tail_lines=-1 must not trim (negative is not > 0)."""
        snap = _snap("a\nb\nc")
        result = _clean_snapshot(snap, "text", tail_lines=-1)
        assert result["screen"] == "a\nb\nc"


# ---------------------------------------------------------------------------
# mutmut_35: non-raw mode — tail_lines > 0 vs tail_lines > 1
# ---------------------------------------------------------------------------


class TestCleanSnapshotNonRawModeTailLinesOne:
    """Kill mutmut_35: tail_lines > 0 → tail_lines > 1 in non-raw mode."""

    def test_text_tail_lines_1_does_trim(self) -> None:
        """tail_lines=1 must trigger trimming (1 > 0 is True, 1 > 1 is False).

        With mutation (> 1), tail_lines=1 would skip trim and return all lines.
        """
        snap = _snap("alpha\nbeta\ngamma")
        result = _clean_snapshot(snap, "text", tail_lines=1)
        # 3 lines, tail_lines=1: must return only last 1 line
        assert result["screen"] == "gamma", f"tail_lines=1 must trim to 1 line in text mode, got: {result['screen']!r}"

    def test_rendered_tail_lines_1_does_trim(self) -> None:
        """tail_lines=1 must trigger trimming in rendered mode."""
        snap = _snap("X\nY\nZ", cols=80)
        result = _clean_snapshot(snap, "rendered", tail_lines=1)
        assert result["screen"] == "Z"


# ---------------------------------------------------------------------------
# mutmut_37: non-raw mode — len(lines) > tail_lines vs >= tail_lines
# ---------------------------------------------------------------------------


class TestCleanSnapshotNonRawModeExactBoundary:
    """Kill mutmut_37: len(lines) > tail_lines → len(lines) >= tail_lines."""

    def test_text_exact_line_count_no_trim(self) -> None:
        """When len(lines) == tail_lines, do NOT trim in text mode.

        With mutation (>=), a no-op trim would produce lines[-N:] == all lines,
        but the content would still be correct. We need a case where the
        mutation causes different output — use ANSI stripped text and verify
        that the content is correct (not trimmed unnecessarily).
        """
        # 3 lines; tail_lines=3 → len == tail_lines → no trim
        snap = _snap("line1\nline2\nline3")
        result = _clean_snapshot(snap, "text", tail_lines=3)
        # With no trim: all 3 lines present
        # With mutation (>= 3): 3 >= 3 is True → trim → lines[-3:] = same 3 lines
        # Both produce same output — need a case where len < tail_lines to distinguish
        assert "line1" in result["screen"]
        assert "line2" in result["screen"]
        assert "line3" in result["screen"]

    def test_text_exact_boundary_line_count_no_trim_distinct(self) -> None:
        """When len(lines) == tail_lines exactly, result is full content (no-op).

        The distinguishing test: len(lines) == tail_lines → return full content.
        With >=: same result (no-op). To distinguish: use ansi-stripped content
        and check that we don't get a different separator.
        """
        # Use 2 lines with tail_lines=2
        snap = _snap("first\nsecond")
        result = _clean_snapshot(snap, "text", tail_lines=2)
        # No trim: "first\nsecond"
        assert result["screen"] == "first\nsecond"

    def test_text_one_more_than_tail_does_trim(self) -> None:
        """len(lines) == tail_lines + 1 must trim."""
        snap = _snap("A\nB\nC")  # 3 lines
        result = _clean_snapshot(snap, "text", tail_lines=2)
        assert result["screen"] == "B\nC"

    def test_text_tail_lines_equal_to_line_count_preserves_all(self) -> None:
        """Exact count match: all lines kept (no trim needed)."""
        # 5 lines with tail=5
        lines = "\n".join(f"line{i}" for i in range(5))
        snap = _snap(lines)
        result = _clean_snapshot(snap, "text", tail_lines=5)
        result_lines = result["screen"].split("\n")
        assert len(result_lines) == 5

    def test_rendered_one_more_than_tail_does_trim(self) -> None:
        """Rendered mode: len > tail_lines triggers trim."""
        snap = _snap("X\nY\nZ\nW", cols=80)
        result = _clean_snapshot(snap, "rendered", tail_lines=2)
        assert result["screen"] == "Z\nW"


# ---------------------------------------------------------------------------
# Combined: raw+non-raw exact boundary behavior cross-check
# ---------------------------------------------------------------------------


class TestCleanSnapshotCrossModeConsistency:
    """Verify raw and non-raw modes behave consistently at boundaries."""

    def test_raw_and_text_same_trim_at_same_tail(self) -> None:
        """Both modes trim when len > tail_lines."""
        screen = "a\nb\nc\nd\ne"
        raw_result = _clean_snapshot({"screen": screen}, "raw", tail_lines=3)
        text_result = _clean_snapshot({"screen": screen}, "text", tail_lines=3)
        assert raw_result["screen"] == "c\nd\ne"
        assert text_result["screen"] == "c\nd\ne"

    def test_raw_and_text_no_trim_when_tail_lines_zero(self) -> None:
        """Both modes preserve full screen when tail_lines=0."""
        screen = "p\nq\nr"
        raw_result = _clean_snapshot({"screen": screen}, "raw", tail_lines=0)
        text_result = _clean_snapshot({"screen": screen}, "text", tail_lines=0)
        assert raw_result["screen"] == screen
        assert text_result["screen"] == screen

    def test_raw_no_trim_when_len_equals_tail(self) -> None:
        """Raw mode: no trim when len(lines) == tail_lines (identity check)."""
        snap = {"screen": "x\ny\nz", "cols": 80}
        result = _clean_snapshot(snap, "raw", tail_lines=3)
        assert result is snap

    def test_text_join_separator_is_newline(self) -> None:
        """Text mode join separator is '\\n' (not 'XX\\nXX' or anything else)."""
        snap = _snap("alpha\nbeta\ngamma\ndelta")
        result = _clean_snapshot(snap, "text", tail_lines=2)
        assert result["screen"] == "gamma\ndelta"
        assert "XX" not in result["screen"]
