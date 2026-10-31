#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Two streams plain pyte raises on, and the state the emulator leaves instead.

A line position in origin mode with no scrolling region asserts; the emulator's
surplus-parameter wrapper swallowed that and left the cursor unclamped, past the
last row. Half of an overwritten wide character makes every later read of the
screen raise IndexError out of ``get_snapshot``. The emulator now leaves what
the Go, C# and TypeScript ports leave for both, so all four agree.
"""

import pyte
import pytest

from provide.uterm.emulator import TerminalEmulator, _TolerantScreen


def _feed(screen: pyte.Screen, data: str) -> pyte.Screen:
    pyte.Stream(screen).feed(data)
    return screen


def test_plain_pyte_asserts_on_a_line_position_in_origin_mode_without_a_region() -> None:
    with pytest.raises(AssertionError):
        _feed(pyte.Screen(10, 4), "\x1b[?6h\x1b[3d")


def test_a_line_position_in_origin_mode_without_a_region_is_absolute() -> None:
    screen = _feed(_TolerantScreen(10, 4), "\x1b[?6h\x1b[1;5H\x1b[3dx")

    assert (screen.cursor.x, screen.cursor.y) == (5, 2)
    assert screen.display[2] == "    x     "


def test_a_line_position_in_origin_mode_without_a_region_is_still_clamped() -> None:
    screen = _feed(_TolerantScreen(10, 4), "\x1b[?6h\x1b[9d")

    assert screen.cursor.y == 3


def test_a_line_position_in_origin_mode_with_a_region_is_still_relative() -> None:
    screen = _feed(_TolerantScreen(10, 6), "\x1b[2;5r\x1b[?6h\x1b[2d")

    assert screen.cursor.y == 2


def test_a_line_position_through_the_emulator() -> None:
    emulator = TerminalEmulator(10, 4, receive_encoding="utf-8")

    emulator.process(b"\x1b[?6h\x1b[3dx")

    assert emulator.get_snapshot()["cursor"] == {"x": 1, "y": 2}


def test_plain_pyte_cannot_display_half_of_an_overwritten_wide_character() -> None:
    screen = _feed(pyte.Screen(6, 2), "中\x1b[1;1Ha")
    with pytest.raises(IndexError):
        _ = screen.display


def test_half_of_an_overwritten_wide_character_displays_as_nothing() -> None:
    screen = _feed(_TolerantScreen(6, 2), "中\x1b[1;1Ha")

    assert screen.display == ["a    ", "      "]


def test_a_wide_character_still_hides_the_cell_after_it() -> None:
    screen = _feed(_TolerantScreen(6, 2), "中\x1b[1;2Hab")

    assert screen.display == ["中b   ", "      "]


def test_an_overwritten_wide_character_through_the_emulator() -> None:
    emulator = TerminalEmulator(6, 2, receive_encoding="utf-8")

    emulator.process("中\x1b[1;1Ha".encode())

    assert emulator.get_snapshot()["screen"] == "a    \n      "


def test_a_character_split_across_two_reads_is_decoded_whole() -> None:
    # A transport hands over whatever a read returned, which can end inside a
    # multibyte character; decoding each read alone made both halves U+FFFD.
    emulator = TerminalEmulator(6, 1, receive_encoding="utf-8")
    data = "中é".encode()

    emulator.process(data[:2])
    emulator.process(data[2:4])
    emulator.process(data[4:])

    assert emulator.get_snapshot()["screen"] == "中é   "


def test_invalid_utf8_is_still_replaced_once_per_maximal_subpart() -> None:
    emulator = TerminalEmulator(6, 1, receive_encoding="utf-8")

    emulator.process(b"\xe2\x82")
    emulator.process(b"A\xff")

    assert emulator.get_snapshot()["screen"] == "�A�   "
