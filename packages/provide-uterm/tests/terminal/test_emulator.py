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
