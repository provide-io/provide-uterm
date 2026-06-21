#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for render.segments — colored text -> structured segments."""

from __future__ import annotations

from provide.uterm.render.segments import (
    Segment,
    ansi_to_segments,
    tokens_to_segments,
)

ESC = "\x1b"


class TestAnsiToSegments:
    def test_plain_text_is_one_default_segment(self) -> None:
        assert ansi_to_segments("hello world") == [Segment("hello world")]

    def test_colored_run_then_reset(self) -> None:
        out = ansi_to_segments(f"pre {ESC}[32mgreen{ESC}[0m post")
        assert out == [
            Segment("pre "),
            Segment("green", color="green"),
            Segment(" post"),
        ]

    def test_bright_fg_is_bold(self) -> None:
        (seg,) = ansi_to_segments(f"{ESC}[92mok")
        assert seg == Segment("ok", color="green", bold=True)

    def test_explicit_bold_flag(self) -> None:
        (seg,) = ansi_to_segments(f"{ESC}[1;33mwarn")
        assert seg == Segment("warn", color="yellow", bold=True)

    def test_adjacent_same_style_runs_merge(self) -> None:
        out = ansi_to_segments(f"{ESC}[36mone {ESC}[36mtwo")
        assert out == [Segment("one two", color="cyan")]

    def test_non_sgr_escapes_are_dropped(self) -> None:
        out = ansi_to_segments(f"{ESC}[2K{ESC}[Hkeep{ESC}[0m")
        assert out == [Segment("keep")]

    def test_extended_color_operands_do_not_leak(self) -> None:
        out = ansi_to_segments(f"{ESC}[38;5;201mx{ESC}[38;2;1;2;3my")
        assert "".join(s.text for s in out) == "xy"

    def test_bare_reset(self) -> None:
        out = ansi_to_segments(f"{ESC}[31mred{ESC}[mplain")
        assert out[0] == Segment("red", color="red")
        assert out[-1] == Segment("plain")

    def test_bold_off_code_22_clears_bold_keeps_color(self) -> None:
        out = ansi_to_segments(f"{ESC}[1;32mbold{ESC}[22mthin")
        assert out == [
            Segment("bold", color="green", bold=True),
            Segment("thin", color="green", bold=False),
        ]

    def test_default_fg_code_39_clears_color_keeps_bold(self) -> None:
        out = ansi_to_segments(f"{ESC}[1;31mred{ESC}[39mdefault")
        assert out == [
            Segment("red", color="red", bold=True),
            Segment("default", color=None, bold=True),
        ]

    def test_truecolor_operands_consumed_then_color_default(self) -> None:
        # 38;2;r;g;b -> default color (we don't map truecolor); operands skipped,
        # so the trailing 'm'-text is not mis-parsed.
        (seg,) = ansi_to_segments(f"{ESC}[38;2;10;20;30mhi")
        assert seg == Segment("hi", color=None, bold=False)

    def test_extended_color_without_operands_is_ignored(self) -> None:
        # 38 with no 5/2 selector: nothing to skip, stays default.
        (seg,) = ansi_to_segments(f"{ESC}[38mx")
        assert seg == Segment("x")

    def test_background_code_48_does_not_set_foreground(self) -> None:
        (seg,) = ansi_to_segments(f"{ESC}[48;5;9mbg")
        assert seg == Segment("bg", color=None, bold=False)

    def test_lone_trailing_escape_is_dropped(self) -> None:
        assert ansi_to_segments(f"tail{ESC}") == [Segment("tail")]

    def test_empty_string_yields_no_segments(self) -> None:
        assert ansi_to_segments("") == []

    def test_unknown_sgr_code_is_ignored(self) -> None:
        # e.g. 7 (reverse video) — not modeled; text stays default.
        (seg,) = ansi_to_segments(f"{ESC}[7mx")
        assert seg == Segment("x")


class TestTokensToSegments:
    def test_brace_tokens_map_to_named_colors(self) -> None:
        # {+g} is bold-green in the dialect; {-x} resets.
        out = tokens_to_segments("Commander {+g}created{-x}!")
        assert out[0].text == "Commander "
        assert out[0].color is None
        assert out[1] == Segment("created", color="green", bold=True)
        assert out[-1].text == "!"

    def test_plain_text_round_trips_unchanged(self) -> None:
        assert tokens_to_segments("no color here") == [Segment("no color here")]

    def test_segments_join_back_to_token_free_text(self) -> None:
        text = "Sector {+c}877{-x} — {+y}StarDock{-x}"
        joined = "".join(s.text for s in tokens_to_segments(text))
        assert joined == "Sector 877 — StarDock"
