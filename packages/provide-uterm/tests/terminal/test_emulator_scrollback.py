#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The emulator hands over the rows a terminal would keep in its scrollback.

It keeps only the visible screen itself. A capture session's previous-session
record wants the history above that screen, so each row that scrolls off the
top of the main screen is handed to a sink, as the plain text a terminal's
scrollback would show.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pyte", reason="pyte not installed; skip emulator tests")

from provide.uterm.emulator import TerminalEmulator


def _emulator(cols: int = 10, rows: int = 3) -> tuple[TerminalEmulator, list[str]]:
    emulator = TerminalEmulator(cols, rows, receive_encoding="utf-8")
    rows_seen: list[str] = []
    emulator.set_scrollback_sink(rows_seen.append)
    return emulator, rows_seen


def test_rows_scrolled_off_the_top_are_handed_over_oldest_first_without_trailing_blanks() -> None:
    emulator, scrolled = _emulator()
    emulator.process(b"one  \r\ntwo\r\nthree\r\nfour\r\nfive")
    assert scrolled == ["one", "two"]
    assert emulator.get_snapshot()["screen"].split("\n") == ["three     ", "four      ", "five      "]


def test_a_line_feed_above_the_bottom_row_scrolls_nothing() -> None:
    emulator, scrolled = _emulator()
    emulator.process(b"one\r\ntwo\r\n")
    assert scrolled == []


def test_autowrap_at_the_bottom_row_scrolls_the_top_row_off() -> None:
    emulator, scrolled = _emulator(cols=4, rows=2)
    emulator.process(b"top\r\nabcdef")
    assert scrolled == ["top"]


def test_the_alternate_screen_hands_nothing_over() -> None:
    for mode in (47, 1047, 1049):
        emulator, scrolled = _emulator()
        emulator.process(f"\x1b[?{mode}h".encode() + b"a\r\nb\r\nc\r\nd\r\ne")
        assert scrolled == [], mode


def test_a_region_that_starts_at_the_top_hands_its_top_row_over() -> None:
    emulator, scrolled = _emulator(rows=4)
    # Rows 1-3 scroll; row 4 (a status line) stays.
    emulator.process(b"\x1b[1;3rA\r\nB\r\nC\r\nD")
    assert scrolled == ["A"]


def test_a_region_lower_down_hands_nothing_over() -> None:
    emulator, scrolled = _emulator(rows=4)
    emulator.process(b"title\x1b[2;4r\x1b[2;1HA\r\nB\r\nC\r\nD")
    assert scrolled == []
    assert emulator.get_snapshot()["screen"].split("\n")[0] == "title     "


def test_a_wide_character_row_is_handed_over_as_it_displays() -> None:
    emulator, scrolled = _emulator(cols=6, rows=1)
    emulator.process("中文\r\n".encode())
    assert scrolled == ["中文"]


def test_a_reset_keeps_the_sink_and_none_stops_it() -> None:
    emulator, scrolled = _emulator(rows=2)
    emulator.reset()
    emulator.process(b"a\r\nb\r\n")
    assert scrolled == ["a"]
    emulator.set_scrollback_sink(None)
    emulator.process(b"c\r\n")
    assert scrolled == ["a"]


def test_without_a_sink_scrolling_is_unchanged() -> None:
    emulator = TerminalEmulator(10, 2, receive_encoding="utf-8")
    emulator.process(b"a\r\nb\r\nc")
    assert emulator.get_snapshot()["screen"].split("\n") == ["b         ", "c         "]
