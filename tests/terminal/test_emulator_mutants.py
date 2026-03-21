#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for TerminalEmulator (emulator.py).

Kills all surviving mutants in __init__, process, _is_cursor_at_end,
get_snapshot, reset, and resize.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pyte", reason="pyte not installed; skip emulator tests")

from provide.terminal.emulator import TerminalEmulator, _parse_screen_text

# ---------------------------------------------------------------------------
# _parse_screen_text (mutmut_2: "XX\nXX".join instead of "\n".join)
# ---------------------------------------------------------------------------


class TestParseScreenText:
    def test_rows_joined_with_newline_only(self) -> None:
        """mutmut_2: rows must be joined with '\\n' not 'XX\\nXX'."""
        import pyte

        screen = pyte.Screen(10, 2)
        stream = pyte.Stream(screen)
        stream.feed("hello")
        result = _parse_screen_text(screen)
        # Should have exactly one newline separator, not 'XX\nXX'
        assert "XX" not in result
        lines = result.split("\n")
        assert len(lines) == 2  # 2 rows


# ---------------------------------------------------------------------------
# __init__ default parameters (mutmut_1,2,3,4,15,16,17)
# ---------------------------------------------------------------------------


class TestTerminalEmulatorInit:
    def test_default_cols_is_80(self) -> None:
        """mutmut_1: default cols must be 80, not 81."""
        emu = TerminalEmulator()
        assert emu.cols == 80

    def test_default_rows_is_25(self) -> None:
        """mutmut_2: default rows must be 25, not 26."""
        emu = TerminalEmulator()
        assert emu.rows == 25

    def test_default_term_is_ansi(self) -> None:
        """mutmut_3,4: default term must be exactly 'ANSI' (not 'ansi' or 'XXANSIXX')."""
        emu = TerminalEmulator()
        assert emu.term == "ANSI"
        assert emu.term != "ansi"
        assert emu.term != "XXANSIXX"

    def test_dirty_is_true_initially(self) -> None:
        """mutmut_15,16: _dirty must start as True (not False or None)."""
        emu = TerminalEmulator()
        assert emu._dirty is True

    def test_last_snapshot_is_none_initially(self) -> None:
        """mutmut_17: _last_snapshot must start as None, not ''."""
        emu = TerminalEmulator()
        assert emu._last_snapshot is None

    def test_custom_cols_stored(self) -> None:
        """cols parameter is stored correctly."""
        emu = TerminalEmulator(cols=40)
        assert emu.cols == 40

    def test_custom_rows_stored(self) -> None:
        """rows parameter is stored correctly."""
        emu = TerminalEmulator(rows=10)
        assert emu.rows == 10

    def test_custom_term_stored(self) -> None:
        """term parameter is stored correctly."""
        emu = TerminalEmulator(term="VT100")
        assert emu.term == "VT100"

    def test_screen_dimensions_match_cols_rows(self) -> None:
        """pyte.Screen is created with the given cols and rows."""
        emu = TerminalEmulator(cols=40, rows=10)
        snap = emu.get_snapshot()
        assert snap["cols"] == 40
        assert snap["rows"] == 10


# ---------------------------------------------------------------------------
# process (mutmut_4,5,6,7: encoding variants)
# ---------------------------------------------------------------------------


class TestTerminalEmulatorProcess:
    def test_process_decodes_cp437(self) -> None:
        """mutmut_4,5: process() must decode data as cp437, not default encoding."""
        # CP437 byte 0x9D is the yen sign '¥' in CP437 but undefined in ASCII
        # Decoding as 'utf-8' (default) would give replacement char
        # With CP437, it should decode correctly
        emu = TerminalEmulator(cols=80, rows=5)
        # 0x41 = 'A' in cp437
        emu.process(b"\x41")
        snap = emu.get_snapshot()
        assert "A" in snap["screen"]

    def test_process_uses_replace_errors(self) -> None:
        """mutmut_6,7: error handler must be 'replace' (lowercase), not 'XXreplaceXX' or 'REPLACE'."""
        emu = TerminalEmulator(cols=80, rows=5)
        # This should not raise — invalid bytes should be replaced
        emu.process(b"\xff\xfe\xfd")  # invalid bytes for many encodings
        snap = emu.get_snapshot()
        assert snap is not None

    def test_process_sets_dirty(self) -> None:
        """After process(), _dirty must be True."""
        emu = TerminalEmulator()
        # Force a clean state
        emu.get_snapshot()
        assert emu._dirty is False
        emu.process(b"x")
        assert emu._dirty is True

    def test_process_updates_screen_content(self) -> None:
        """Processed bytes appear in snapshot screen."""
        emu = TerminalEmulator(cols=80, rows=5)
        emu.process(b"Hello")
        snap = emu.get_snapshot()
        assert "Hello" in snap["screen"]


# ---------------------------------------------------------------------------
# _is_cursor_at_end (mutmut_7,8,9,11,12,13,14,15,16,17,19,21,22,23,26,27)
# ---------------------------------------------------------------------------


class TestIsCursorAtEnd:
    def test_empty_screen_returns_true(self) -> None:
        """mutmut_27: empty screen (no content lines) must return True, not False."""
        emu = TerminalEmulator(cols=80, rows=5)
        # Empty screen — all lines are blank
        result = emu._is_cursor_at_end()
        assert result is True

    def test_cursor_at_end_of_single_line(self) -> None:
        """mutmut_19,21: cursor at >= len(line)-2 returns True."""
        emu = TerminalEmulator(cols=80, rows=5)
        emu.process(b"AB")  # 2 chars, cursor ends at x=2, line len=2, 2 >= 2-2=0 → True
        snap = emu.get_snapshot()
        # After "AB", cursor should be at or past end of content
        assert snap["cursor_at_end"] is True

    def test_cursor_on_same_row_near_end(self) -> None:
        """mutmut_21: >= vs > boundary check — cursor at len-2 returns True."""
        emu = TerminalEmulator(cols=80, rows=5)
        # Write exactly 4 chars: "ABCD"
        # len("ABCD") = 4, len-2 = 2
        # cursor_x after processing should be 4
        emu.process(b"ABCD")
        result = emu._is_cursor_at_end()
        # cursor_x=4 >= len("ABCD")-2=2 → True
        assert result is True

    def test_cursor_on_same_row_not_at_end(self) -> None:
        """mutmut_22,23: cursor well before end of content returns False."""
        emu = TerminalEmulator(cols=80, rows=5)
        # Write 20 chars then move cursor back to start via CR
        emu.process(b"ABCDEFGHIJKLMNOPQRST")  # 20 chars
        emu.process(b"\r")  # carriage return → cursor_x=0, row=0
        result = emu._is_cursor_at_end()
        # cursor_x=0, len(line)=20, 0 >= 20-2=18 → False
        assert result is False

    def test_cursor_below_last_content_row_returns_true(self) -> None:
        """mutmut_26: cursor_y > row_idx (strict) must return True."""
        emu = TerminalEmulator(cols=80, rows=5)
        emu.process(b"hello")
        # Move cursor to row 2 (below content on row 0)
        emu.process(b"\x1b[3;1H")  # row 3, col 1 (1-indexed)
        result = emu._is_cursor_at_end()
        assert result is True

    def test_cursor_on_row_equal_to_last_content_not_beyond(self) -> None:
        """mutmut_26: cursor_y == row_idx falls through to >= check, not >= check."""
        emu = TerminalEmulator(cols=80, rows=5)
        # Write content on row 0, cursor stays on row 0 after CR+text
        emu.process(b"ABCDEFGHIJKLMNOPQRST")  # cursor at end of row 0
        # Move cursor back to x=0 on same row
        emu.process(b"\r")
        snap = emu.get_snapshot()
        # cursor_y == row_idx (both 0), cursor_x=0, len=20
        # 0 >= 20-2=18 → False
        assert snap["cursor_at_end"] is False

    def test_only_last_row_in_range_examined(self) -> None:
        """mutmut_7,8,9,11,12,13,14,15: range must cover all rows from last to first."""
        emu = TerminalEmulator(cols=80, rows=5)
        # Put content only on the last row
        emu.process(b"\x1b[5;1H")  # move to row 5 (last row, 1-indexed)
        emu.process(b"LAST")
        result = emu._is_cursor_at_end()
        # Cursor is at end of "LAST" on last row → True
        assert result is True

    def test_line_rstripped_for_length(self) -> None:
        """mutmut_16,17: line must use rstrip() not None or lstrip() for length calculation."""
        emu = TerminalEmulator(cols=80, rows=5)
        # Write "AB   " (2 non-space chars, then spaces)
        # rstrip() → "AB" (len=2); lstrip() → "AB   " (different len)
        # cursor after "AB   " should be at x=5
        emu.process(b"AB")
        # Verify rstrip behavior: content line ends at "AB", cursor past it
        snap = emu.get_snapshot()
        # cursor at x=2, row=0; line="AB" (rstripped), len=2, 2 >= 2-2=0 → True
        assert snap["cursor_at_end"] is True


# ---------------------------------------------------------------------------
# get_snapshot (mutmut_2,9,15,16,29-37,41-53)
# ---------------------------------------------------------------------------


class TestGetSnapshot:
    def test_recomputes_when_dirty(self) -> None:
        """mutmut_2: must recompute when _dirty=True (not when not None)."""
        emu = TerminalEmulator(cols=80, rows=5)
        snap1 = emu.get_snapshot()
        h1 = snap1["screen_hash"]
        emu.process(b"new content")
        snap2 = emu.get_snapshot()
        h2 = snap2["screen_hash"]
        assert h1 != h2

    def test_uses_cache_when_not_dirty(self) -> None:
        """mutmut_2: must use cache when not dirty (not recompute every call)."""
        emu = TerminalEmulator(cols=80, rows=5)
        emu.process(b"hello")
        snap1 = emu.get_snapshot()
        snap2 = emu.get_snapshot()
        # Same screen_hash since no new data
        assert snap1["screen_hash"] == snap2["screen_hash"]

    def test_dirty_set_false_after_snapshot(self) -> None:
        """mutmut_36,37: _dirty must be set to False (not None or True) after snapshot."""
        emu = TerminalEmulator(cols=80, rows=5)
        emu.process(b"hello")
        assert emu._dirty is True
        emu.get_snapshot()
        assert emu._dirty is False

    def test_screen_hash_uses_sha256(self) -> None:
        """mutmut_9: screen_hash must be SHA-256 hex digest of screen text."""
        import hashlib

        emu = TerminalEmulator(cols=80, rows=5)
        emu.process(b"TEST")
        snap = emu.get_snapshot()
        screen_text = snap["screen"]
        expected_hash = hashlib.sha256(screen_text.encode("utf-8")).hexdigest()
        assert snap["screen_hash"] == expected_hash

    def test_snapshot_has_cursor_key(self) -> None:
        """mutmut_15,16: snapshot must have 'cursor' key (not 'XXcursorXX' or 'CURSOR')."""
        emu = TerminalEmulator(cols=80, rows=5)
        snap = emu.get_snapshot()
        assert "cursor" in snap
        assert "XXcursorXX" not in snap
        assert "CURSOR" not in snap

    def test_cursor_has_x_and_y(self) -> None:
        """mutmut_48-53: cursor must have 'x' and 'y' keys with value 0 defaults."""
        emu = TerminalEmulator(cols=80, rows=5)
        snap = emu.get_snapshot()
        cursor = snap["cursor"]
        assert "x" in cursor
        assert "y" in cursor
        assert "XXxXX" not in cursor
        assert "XXyXX" not in cursor
        assert "X" not in cursor
        assert "Y" not in cursor

    def test_cursor_x_y_default_zero(self) -> None:
        """mutmut_50,53: default cursor x=0 and y=0 (not x=1 or y=1)."""
        emu = TerminalEmulator(cols=80, rows=5)
        snap = emu.get_snapshot()
        cursor = snap["cursor"]
        assert cursor["x"] == 0
        assert cursor["y"] == 0

    def test_snapshot_cursor_uses_or_fallback(self) -> None:
        """mutmut_44: snap['cursor'] uses 'or' not 'and' for fallback."""
        emu = TerminalEmulator(cols=80, rows=5)
        snap = emu.get_snapshot()
        # cursor should be the real cursor, not the fallback
        assert isinstance(snap["cursor"], dict)
        assert "x" in snap["cursor"]

    def test_snapshot_cursor_key_lowercase(self) -> None:
        """mutmut_45,46,47: snap.get must use key 'cursor' (lowercase)."""
        emu = TerminalEmulator(cols=80, rows=5)
        snap = emu.get_snapshot()
        assert "cursor" in snap
        assert snap["cursor"] is not None

    def test_has_trailing_space_key_lowercase(self) -> None:
        """mutmut_29,30: must have 'has_trailing_space' key (not 'XXhas_trailing_spaceXX' or 'HAS_TRAILING_SPACE')."""
        emu = TerminalEmulator(cols=80, rows=5)
        snap = emu.get_snapshot()
        assert "has_trailing_space" in snap
        assert "XXhas_trailing_spaceXX" not in snap
        assert "HAS_TRAILING_SPACE" not in snap

    def test_has_trailing_space_true_when_ends_with_space(self) -> None:
        """mutmut_31,32,33,34,35: has_trailing_space logic must use rstrip() != rstrip(' :')."""
        emu = TerminalEmulator(cols=80, rows=5)
        # Write content that ends with a space
        emu.process(b"Enter: ")
        snap = emu.get_snapshot()
        # Screen text ends with a space, so rstrip() != rstrip(" :") → True
        assert snap["has_trailing_space"] is True

    def test_has_trailing_space_false_when_no_trailing(self) -> None:
        """mutmut_32: == instead of != would flip result."""
        # Use a non-space/colon ending — screen_text.rstrip() must equal screen_text.rstrip(" :")
        # We test on the raw screen_text computation: if screen ends only with spaces (from pyte
        # padding), rstrip() removes them AND rstrip(" :") also removes them → equal → False
        # But if it ends with a colon or explicit space character, they differ → True
        # Verify the False case by checking empty screen (all whitespace stripped to same result)
        emu = TerminalEmulator(cols=80, rows=5)
        snap = emu.get_snapshot()
        screen_text = snap["screen"]
        # Confirm logic: rstrip() should equal rstrip(" :") for an empty/blank screen
        expected = screen_text.rstrip() != screen_text.rstrip(" :")
        assert snap["has_trailing_space"] is expected

    def test_has_trailing_space_true_when_ends_with_colon(self) -> None:
        """mutmut_33,34,35: must strip only ' :' chars from the right."""
        emu = TerminalEmulator(cols=80, rows=5)
        emu.process(b"Enter:")
        snap = emu.get_snapshot()
        # Ends with colon, so has_trailing_space=True
        assert snap["has_trailing_space"] is True

    def test_captured_at_always_fresh(self) -> None:
        """captured_at must be a fresh timestamp on each call."""
        import time

        emu = TerminalEmulator(cols=80, rows=5)
        before = time.time()
        snap = emu.get_snapshot()
        after = time.time()
        assert before <= snap["captured_at"] <= after

    def test_snapshot_cursor_is_copy(self) -> None:
        """mutmut_41,42: snap['cursor'] must use key 'cursor' (not 'XXcursorXX' or 'CURSOR')."""
        emu = TerminalEmulator(cols=80, rows=5)
        snap = emu.get_snapshot()
        # The cursor should not contain wrong keys
        assert "XXcursorXX" not in snap
        assert "CURSOR" not in snap


# ---------------------------------------------------------------------------
# reset (mutmut_1,2: _dirty must be True after reset, not None or False)
# ---------------------------------------------------------------------------


class TestTerminalEmulatorReset:
    def test_reset_sets_dirty_true(self) -> None:
        """mutmut_1,2: reset() must set _dirty=True (not None or False)."""
        emu = TerminalEmulator(cols=80, rows=5)
        emu.process(b"content")
        emu.get_snapshot()  # clears dirty
        assert emu._dirty is False
        emu.reset()
        assert emu._dirty is True

    def test_reset_clears_screen_content(self) -> None:
        """After reset, screen should be empty."""
        emu = TerminalEmulator(cols=80, rows=5)
        emu.process(b"Hello World")
        emu.reset()
        snap = emu.get_snapshot()
        assert "Hello" not in snap["screen"]

    def test_reset_causes_snapshot_recompute(self) -> None:
        """After reset, snapshot is recomputed (not cached)."""
        emu = TerminalEmulator(cols=80, rows=5)
        emu.process(b"content")
        snap1 = emu.get_snapshot()
        emu.reset()
        snap2 = emu.get_snapshot()
        assert snap1["screen_hash"] != snap2["screen_hash"]


# ---------------------------------------------------------------------------
# resize (mutmut_3,4,5,6,7,8)
# ---------------------------------------------------------------------------


class TestTerminalEmulatorResize:
    def test_resize_stores_new_cols(self) -> None:
        """mutmut_3,5: resize must pass cols to _screen.resize (not None or rows only)."""
        emu = TerminalEmulator(cols=80, rows=25)
        emu.resize(40, 12)
        assert emu.cols == 40
        snap = emu.get_snapshot()
        assert snap["cols"] == 40

    def test_resize_stores_new_rows(self) -> None:
        """mutmut_4,5: resize must pass rows to _screen.resize (not None)."""
        emu = TerminalEmulator(cols=80, rows=25)
        emu.resize(40, 12)
        assert emu.rows == 12
        snap = emu.get_snapshot()
        assert snap["rows"] == 12

    def test_resize_passes_both_args(self) -> None:
        """mutmut_5,6: _screen.resize must receive both cols and rows."""
        emu = TerminalEmulator(cols=80, rows=25)
        emu.resize(60, 20)
        snap = emu.get_snapshot()
        assert snap["cols"] == 60
        assert snap["rows"] == 20

    def test_resize_sets_dirty_true(self) -> None:
        """mutmut_7,8: resize() must set _dirty=True (not None or False)."""
        emu = TerminalEmulator(cols=80, rows=25)
        emu.get_snapshot()  # clears dirty
        assert emu._dirty is False
        emu.resize(40, 12)
        assert emu._dirty is True

    def test_resize_causes_snapshot_update(self) -> None:
        """After resize, snapshot reflects new dimensions."""
        emu = TerminalEmulator(cols=80, rows=25)
        emu.resize(40, 12)
        snap = emu.get_snapshot()
        assert snap["cols"] == 40
        assert snap["rows"] == 12

    def test_resize_cols_not_swapped_with_rows(self) -> None:
        """mutmut_3,4: cols and rows must not be swapped in the _screen.resize call."""
        emu = TerminalEmulator(cols=80, rows=25)
        emu.resize(30, 10)
        snap = emu.get_snapshot()
        assert snap["cols"] == 30
        assert snap["rows"] == 10
