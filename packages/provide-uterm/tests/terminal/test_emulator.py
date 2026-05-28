#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for TerminalEmulator."""

from __future__ import annotations

import pytest

pytest.importorskip("pyte", reason="pyte not installed; skip emulator tests")

from provide.uterm.emulator import TerminalEmulator


class TestTerminalEmulator:
    def test_initial_snapshot_empty(self) -> None:
        emu = TerminalEmulator(cols=80, rows=5)
        snap = emu.get_snapshot()
        assert snap["cols"] == 80
        assert snap["rows"] == 5
        assert snap["term"] == "ANSI"
        assert "screen" in snap
        assert "screen_hash" in snap
        assert "cursor" in snap

    def test_process_updates_screen(self) -> None:
        emu = TerminalEmulator(cols=80, rows=5)
        emu.process(b"Hello")
        snap = emu.get_snapshot()
        assert "Hello" in snap["screen"]

    def test_hash_changes_on_update(self) -> None:
        emu = TerminalEmulator(cols=80, rows=5)
        h1 = emu.get_snapshot()["screen_hash"]
        emu.process(b"New content")
        h2 = emu.get_snapshot()["screen_hash"]
        assert h1 != h2

    def test_reset_clears_screen(self) -> None:
        emu = TerminalEmulator(cols=80, rows=5)
        emu.process(b"Hello World")
        emu.reset()
        snap = emu.get_snapshot()
        assert "Hello" not in snap["screen"]

    def test_resize(self) -> None:
        emu = TerminalEmulator(cols=80, rows=25)
        emu.resize(40, 12)
        snap = emu.get_snapshot()
        assert snap["cols"] == 40
        assert snap["rows"] == 12

    def test_resize_preserves_geometry(self) -> None:
        # The underlying pyte buffer geometry must match the requested
        # (cols, rows), not a transposed (rows, cols). Exercise the buffer
        # directly: a full-width row must not be clipped at the row count.
        emu = TerminalEmulator(cols=80, rows=24)
        emu.resize(120, 40)  # cols=120, rows=40
        assert emu.cols == 120
        assert emu.rows == 40
        # pyte's screen geometry: lines == rows, columns == cols.
        assert emu._screen.lines == 40
        assert emu._screen.columns == 120
        emu.process(b"x" * 120 + b"\r\n")
        display = emu._screen.display
        assert len(display) == 40  # rows
        assert display[0].rstrip() == "x" * 120  # full width, not clipped at 40

    def test_snapshot_has_captured_at(self) -> None:
        emu = TerminalEmulator()
        snap = emu.get_snapshot()
        assert "captured_at" in snap
        assert snap["captured_at"] > 0

    def test_cursor_position(self) -> None:
        emu = TerminalEmulator(cols=80, rows=5)
        snap = emu.get_snapshot()
        assert "x" in snap["cursor"]
        assert "y" in snap["cursor"]

    def test_cursor_below_last_content_row(self) -> None:
        # Process content on row 0, then move cursor to row 1 (below content).
        # _is_cursor_at_end returns True (cursor_y > last content row_idx).
        import pyte  # noqa: F401

        emu = TerminalEmulator(cols=80, rows=5)
        emu.process(b"hello")
        # Move cursor to next line explicitly via ANSI
        emu.process(b"\x1b[2;1H")  # move cursor to row 2 (1-indexed)
        snap = emu.get_snapshot()
        assert snap["cursor_at_end"] is True


class TestTerminalEmulatorAnsiScreen:
    """Cover TerminalEmulator.ansi_screen (emulator.py lines 133-162).

    The method walks pyte's style buffer and emits SGR escape sequences
    on style transitions, so exercising it with both a styled and an
    empty terminal covers every branch in the per-cell loop:

    - empty cell vs cell with content (lines 142-155),
    - style equal vs differing from ``last_style`` (lines 156-158),
    - row terminator (line 160), and
    - the row-join return (line 162).
    """

    def test_ansi_screen_empty_terminal_emits_reset_only(self) -> None:
        """Bare terminal — every row is filled with the default style and
        terminated with the ANSI reset escape sequence."""
        from provide.uterm.render.buffer import ANSI_RESET

        emu = TerminalEmulator(cols=4, rows=2)
        out = emu.ansi_screen()
        # Two rows, each terminated by ANSI_RESET, joined by a single newline.
        rows = out.split("\n")
        assert len(rows) == 2
        for row in rows:
            assert row.endswith(ANSI_RESET)

    def test_get_snapshot_reuses_cached_when_not_dirty(self) -> None:
        """Second consecutive ``get_snapshot()`` with no intervening change
        must skip the screen-rebuild block (covers the 99->114 branch where
        ``_last_snapshot`` is not None and ``_dirty`` is False)."""
        emu = TerminalEmulator(cols=10, rows=2)
        first = emu.get_snapshot()
        # No process() between calls, so the cache must be reused.
        second = emu.get_snapshot()
        # Same screen content + hash; only captured_at differs.
        assert first["screen"] == second["screen"]
        assert first["screen_hash"] == second["screen_hash"]

    def test_ansi_screen_with_styled_text_emits_sgr_transition(self) -> None:
        """When the terminal has styled content, the output contains both
        an SGR escape and the literal text — covering the style-change
        path inside the per-cell loop."""
        emu = TerminalEmulator(cols=20, rows=2)
        # Set red foreground, then write text, then reset, then more text.
        emu.process(b"\x1b[31mRED\x1b[0m PLAIN")
        out = emu.ansi_screen()
        # SGR escape is present; literal text is preserved.
        assert "\x1b[" in out
        assert "RED" in out
        assert "PLAIN" in out
