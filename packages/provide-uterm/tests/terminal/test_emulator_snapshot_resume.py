#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A snapshot must leave a viewer's terminal where the live stream expects it.

A viewer joining a live session is painted from ``ansi_screen()`` and then fed
the rest of the stream. Nothing restarts for it: every byte after the snapshot
assumes the cursor, pen and modes the bytes before it left behind. A program
that paints with relative movement (vitetris never sends CUP) drew its whole
board in the wrong place when the snapshot left the cursor at the bottom-right
of the drawn text instead of where the program's cursor really was.

So the contract tested here is end-to-end, not a byte layout: snapshot at a
split point, paint a fresh terminal with it, feed the remainder, and the result
must be the screen, cursor, pen and modes that the uninterrupted stream gives.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("pyte", reason="pyte not installed; skip emulator tests")

from provide.uterm.emulator import TerminalEmulator

_FIXTURE = Path(__file__).resolve().parents[4] / "conformance" / "snapshot" / "relative_paint_80x24.bin"

#: One terminal token: a CSI sequence, a charset designation, any other escape,
#: or a single byte. A split is only taken between tokens: a snapshot is sent
#: between chunks of output, and a viewer handed half an escape sequence would
#: print the other half whatever the snapshot said.
_TOKEN = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b.|.", re.DOTALL)


def _paint(snapshot: str, encoding: str) -> bytes:
    # What the browser element does with a snapshot frame's screen: rows are
    # joined with a bare LF, which it turns into CRLF before writing.
    return snapshot.replace("\n", "\r\n").encode(encoding)


def _emulator(encoding: str, cols: int = 80, rows: int = 24) -> TerminalEmulator:
    return TerminalEmulator(cols=cols, rows=rows, receive_encoding=encoding)


def _resume(before: bytes, after: bytes, encoding: str, cols: int = 80, rows: int = 24) -> TerminalEmulator:
    live = _emulator(encoding, cols, rows)
    live.process(before)
    viewer = _emulator(encoding, cols, rows)
    viewer.process(_paint(live.ansi_screen(), encoding))
    viewer.process(after)
    return viewer


def _straight(data: bytes, encoding: str, cols: int = 80, rows: int = 24) -> TerminalEmulator:
    emulator = _emulator(encoding, cols, rows)
    emulator.process(data)
    return emulator


def _state(emulator: TerminalEmulator) -> tuple[str, dict[str, int], str]:
    snapshot = emulator.get_snapshot()
    # ansi_screen() carries every cell's style plus the restored cursor, pen and
    # modes, so equal ansi_screen() output is equal terminal state.
    return snapshot["screen"], snapshot["cursor"], emulator.ansi_screen()


def _assert_resumes(data: bytes, split: int, encoding: str = "utf-8", cols: int = 80, rows: int = 24) -> None:
    expected = _state(_straight(data, encoding, cols, rows))
    actual = _state(_resume(data[:split], data[split:], encoding, cols, rows))
    assert actual == expected, f"split at byte {split} ({encoding})"


def _split_points(data: bytes) -> list[int]:
    return [match.end() for match in _TOKEN.finditer(data)]


@pytest.fixture(scope="module")
def data() -> bytes:
    return _FIXTURE.read_bytes()


class TestRelativePaintFixture:
    """The committed relative-movement stream, resumed at many split points."""

    def test_the_fixture_is_the_one_every_port_replays(self, data: bytes) -> None:
        assert len(data) == 4634
        assert data.isascii()

    @pytest.mark.parametrize("encoding", ["utf-8", "cp437"])
    def test_resumes_identically_at_every_seventh_token(self, data: bytes, encoding: str) -> None:
        points = _split_points(data)
        # Every seventh token boundary plus both ends: hundreds of distinct
        # states, kept to a few seconds of pyte (all of them pass; they take a minute).
        chosen = sorted({0, len(data), *points[::7]})
        assert len(chosen) > 200
        for split in chosen:
            _assert_resumes(data, split, encoding)

    def test_the_fixture_actually_needs_the_restored_state(self, data: bytes) -> None:
        """Painting the screen alone, with no state after it, must diverge.

        Guards the fixture rather than the code: if it stopped depending on
        cursor and pen state, the resume test above would pass vacuously.
        """
        split = len(data) // 2
        live = _emulator("utf-8")
        live.process(data[:split])
        viewer = _emulator("utf-8")
        viewer.process(_paint(_rows(live), "utf-8"))
        viewer.process(data[split:])
        assert _state(viewer)[:2] != _state(_straight(data, "utf-8"))[:2]


def _rows(emulator: TerminalEmulator) -> str:
    """The painted rows of a snapshot without the state that follows them."""
    from provide.uterm.render.buffer import render_cell_rows

    return "\n".join(render_cell_rows(emulator._screen.buffer, emulator.cols, emulator.rows))


class TestCursor:
    def test_the_cursor_is_left_where_the_program_left_it(self) -> None:
        live = _emulator("utf-8", 20, 5)
        live.process(b"top\r\n\x1b[2Bmid\x1b[3D")
        suffix = live.ansi_screen().rsplit("\n", 1)[1]
        assert "\x1b[4;1H" in suffix
        stream = b"top\r\n\x1b[2Bmid\x1b[3D"
        _assert_resumes(stream + b"\x1b[1Aup", len(stream), cols=20, rows=5)

    def test_a_hidden_cursor_stays_hidden(self) -> None:
        live = _emulator("utf-8", 20, 5)
        live.process(b"\x1b[?25l")
        assert live.ansi_screen().endswith("\x1b[?25l")
        live.process(b"\x1b[?25h")
        assert live.ansi_screen().endswith("\x1b[?25h")


class TestPen:
    def test_every_attribute_and_both_colours_are_restored(self) -> None:
        live = _emulator("utf-8", 20, 5)
        live.process(b"\x1b[1;3;4;5;7;9;38;5;208;48;2;1;2;3mX")
        assert "\x1b[0;1;3;4;5;7;9;38;2;255;135;0;48;2;1;2;3m" in live.ansi_screen()

    def test_named_colours_including_the_ones_pyte_calls_brown(self) -> None:
        live = _emulator("utf-8", 20, 5)
        live.process(b"\x1b[33;103mX")
        assert "\x1b[0;33;103m" in live.ansi_screen()

    def test_a_default_pen_is_an_explicit_reset(self) -> None:
        live = _emulator("utf-8", 20, 5)
        live.process(b"\x1b[31mX\x1b[m")
        assert "\x1b[0m\x1b[?25h" in live.ansi_screen()

    def test_drawing_after_resume_uses_the_live_pen(self) -> None:
        # The background bled: vitetris sets the pen, moves, then draws.
        before = b"\x1b[44;1;37m\x1b[5C"
        _assert_resumes(before + b"\x1b[1Bboard", len(before), cols=20, rows=5)


class TestCells:
    def test_reverse_video_is_sent_as_reverse_not_as_swapped_colours(self) -> None:
        # Swapping default for default is no change at all, so the reversed
        # cell vitetris marks the selected level with used to vanish.
        live = _emulator("utf-8", 10, 2)
        live.process(b"\x1b[7m0\x1b[m")
        assert live.ansi_screen().startswith("\x1b[0;7m0")

    def test_reverse_with_colours_keeps_them_in_place(self) -> None:
        live = _emulator("utf-8", 10, 2)
        live.process(b"\x1b[7;31;42m0")
        assert live.ansi_screen().startswith("\x1b[0;7;31;42m0")

    def test_italic_and_strikethrough_cells_are_drawn_italic_and_struck(self) -> None:
        # The pen carried them, but a drawn cell lost them: a viewer that
        # joined saw plain text where everyone else saw italic or struck text.
        live = _emulator("utf-8", 10, 2)
        live.process(b"\x1b[3mI\x1b[0;9mS\x1b[0;1;3;4;5;7;9mA\x1b[m")
        assert live.ansi_screen().startswith("\x1b[0;3mI\x1b[0;9mS\x1b[0;1;3;4;5;7;9mA\x1b[0m ")

    def test_italic_and_strikethrough_cells_survive_a_resume(self) -> None:
        before = b"\x1b[3mit\x1b[23;9mst\x1b[m plain"
        _assert_resumes(before + b"\x1b[3m!", len(before), cols=20, rows=5)


class TestScrollRegion:
    def test_margins_and_origin_mode_are_restored(self) -> None:
        live = _emulator("utf-8", 20, 10)
        live.process(b"\x1b[3;7r\x1b[?6h\x1b[2;4H")
        suffix = live.ansi_screen().rsplit("\n", 1)[1]
        assert "\x1b[3;7r\x1b[?6h" in suffix
        # Origin mode addresses rows from the top margin.
        assert "\x1b[2;4H" in suffix

    def test_no_region_is_an_explicit_full_screen_region(self) -> None:
        live = _emulator("utf-8", 20, 10)
        suffix = live.ansi_screen().rsplit("\n", 1)[1]
        assert "\x1b[r\x1b[?6l" in suffix
        # A region set to the whole screen is the same thing, sent the same way.
        live.process(b"\x1b[1;10r")
        assert "\x1b[r\x1b[?6l" in live.ansi_screen().rsplit("\n", 1)[1]

    @pytest.mark.parametrize("origin", [b"", b"\x1b[?6h"])
    def test_scrolling_inside_the_region_after_resume(self, origin: bytes) -> None:
        stream = b"line1\r\n\x1b[2;5r" + origin + b"\x1b[3Bin\r\n\r\n\r\nscrolled\r\nagain"
        for split in _split_points(stream):
            _assert_resumes(stream, split, cols=20, rows=8)


class TestPendingWrap:
    def test_the_deferred_wrap_survives(self) -> None:
        stream = b"\x1b[2;1H" + b"x" * 19 + b"\x1b[32mY" + b"\x1b[35mwrapped"
        live = _emulator("utf-8", 20, 4)
        live.process(stream[: stream.index(b"\x1b[35m")])
        assert live.get_snapshot()["cursor"] == {"x": 20, "y": 1}
        # The last cell is drawn again, in its own style, to land the cursor
        # in the deferred-wrap state rather than on the last column.
        assert "\x1b[2;20H\x1b[0;32mY" in live.ansi_screen()
        for split in _split_points(stream):
            _assert_resumes(stream, split, cols=20, rows=4)

    def test_a_wide_character_in_the_last_two_columns(self) -> None:
        stream = "\x1b[1;19H中after".encode()
        live = _emulator("utf-8", 20, 3)
        live.process(stream[: -len(b"after")])
        assert live.get_snapshot()["cursor"] == {"x": 20, "y": 0}
        assert "\x1b[1;19H\x1b[0m中" in live.ansi_screen()
        _assert_resumes(stream, len(stream) - len(b"after"), cols=20, rows=3)

    def test_with_origin_mode_the_row_is_relative_to_the_margin(self) -> None:
        stream = b"\x1b[2;4r\x1b[?6h\x1b[2;1H" + b"z" * 20 + b"next"
        live = _emulator("utf-8", 20, 6)
        live.process(stream[:-4])
        assert "\x1b[2;20H" in live.ansi_screen().rsplit("\n", 1)[1]
        _assert_resumes(stream, len(stream) - 4, cols=20, rows=6)

    def test_without_autowrap_the_last_cell_is_overwritten(self) -> None:
        stream = b"\x1b[?7l" + b"a" * 20 + b"bc"
        live = _emulator("utf-8", 20, 3)
        live.process(stream[:-2])
        assert "\x1b[?7l" in live.ansi_screen().rsplit("\n", 1)[1]
        _assert_resumes(stream, len(stream) - 2, cols=20, rows=3)


class TestModes:
    def test_insert_mode(self) -> None:
        stream = b"abcdef\r\x1b[4hXY"
        live = _emulator("utf-8", 20, 3)
        live.process(stream[:-2])
        assert "\x1b[4h" in live.ansi_screen().rsplit("\n", 1)[1]
        _assert_resumes(stream, len(stream) - 2, cols=20, rows=3)

    def test_line_feed_new_line_mode(self) -> None:
        stream = b"\x1b[20h\x1b[5Cab\ncd"
        live = _emulator("utf-8", 20, 3)
        live.process(stream[:-3])
        assert "\x1b[20h" in live.ansi_screen()
        _assert_resumes(stream, len(stream) - 3, cols=20, rows=3)

    def test_input_modes_a_controlling_viewer_must_honour(self) -> None:
        live = _emulator("utf-8", 20, 3)
        assert "\x1b[?1l" in live.ansi_screen()
        live.process(b"\x1b[?1h\x1b[?2004h\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h")
        suffix = live.ansi_screen().rsplit("\n", 1)[1]
        assert "\x1b[?1h\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h\x1b[?2004h" in suffix

    def test_the_alternate_screen_is_entered_before_the_paint(self) -> None:
        live = _emulator("utf-8", 20, 3)
        live.process(b"\x1b[?1049hgame")
        assert live.ansi_screen().startswith("\x1b[?1049h\x1b[H")
        live.process(b"\x1b[?1049l")
        assert not live.ansi_screen().startswith("\x1b[?")


class TestCharsets:
    def test_designations_and_the_shift_are_restored_in_a_utf8_session(self) -> None:
        stream = b"\x1b)0\x0eqqq\x1b(0x"
        live = _emulator("utf-8", 20, 3)
        live.process(stream)
        suffix = live.ansi_screen().rsplit("\n", 1)[1]
        assert "\x1b(0\x1b)0\x0e" in suffix
        _assert_resumes(stream + b"qq\x0fqq\x1b(Bqq", len(stream), cols=20, rows=3)

    def test_the_power_on_designations_are_not_sent(self) -> None:
        # A snapshot is also read as text, and strip_ansi leaves designations
        # in; so pyte's power-on state (G0 B, G1 0, shifted in) sends nothing
        # while nothing has designated a set.
        live = _emulator("utf-8", 20, 3)
        suffix = live.ansi_screen().rsplit("\n", 1)[1]
        assert "\x1b(" not in suffix
        assert "\x1b)" not in suffix
        live.process(b"\x1b)B")
        assert "\x1b(B\x1b)B" in live.ansi_screen()

    def test_a_designation_matching_the_power_on_state_is_still_sent(self) -> None:
        # curses under TERM=screen designates G1 as graphics once, then draws
        # every line with SO/SI. pyte's G1 already was graphics; a viewer's is
        # ASCII, and would draw letters at the next SO.
        stream = b"\x1b(B\x1b)0\x0eqq\x0f"
        live = _emulator("utf-8", 20, 3)
        live.process(stream)
        assert "\x1b(B\x1b)0\x1b[0m" in live.ansi_screen()
        _assert_resumes(stream + b"\x0eqqqq\x0f", len(stream), cols=20, rows=3)
        # A reset forgets it.
        live.process(b"\x1bc")
        assert "\x1b)" not in live.ansi_screen()

    def test_shifted_out_on_the_default_g1_is_sent(self) -> None:
        live = _emulator("utf-8", 20, 3)
        live.process(b"\x0e")
        assert "\x1b(B\x1b)0\x0e" in live.ansi_screen()

    def test_every_designation_pyte_knows_maps_back(self) -> None:
        for code in "B0UV":
            live = _emulator("utf-8", 20, 3)
            live.process(b"\x1b(" + code.encode() + b"\x1b)" + code.encode())
            assert f"\x1b({code}\x1b){code}" in live.ansi_screen()

    def test_a_cp437_session_sends_no_charset_state(self) -> None:
        # pyte ignores designations and shifts in a CP437 session, so there is
        # no state to restore -- and a stray SO in ANSI art stays a literal.
        live = _emulator("cp437", 20, 3)
        live.process(b"\x1b)0\x0e")
        suffix = live.ansi_screen().rsplit("\n", 1)[1]
        assert "\x1b(" not in suffix
        assert "\x1b)" not in suffix
        assert "\x0e" not in suffix
