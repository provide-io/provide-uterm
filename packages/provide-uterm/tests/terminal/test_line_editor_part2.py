#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for LineEditor — readline-style terminal line editor."""

from __future__ import annotations

from provide.uterm.line_editor import LineEditor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _editor(**kwargs) -> tuple[LineEditor, list[str]]:
    """Return (editor, writes_list); writes appended via on_write callback."""
    writes: list[str] = []

    async def _write(data: str) -> None:
        writes.append(data)

    return LineEditor(on_write=_write, **kwargs), writes


def _joined(writes: list[str]) -> str:
    """Concatenate all writes for pattern-checking."""
    return "".join(writes)


# ---------------------------------------------------------------------------
# Enter / line completion
# ---------------------------------------------------------------------------


class TestMidlineInsert:
    async def test_insert_at_start(self) -> None:
        ed, writes = _editor()
        for ch in "bc":
            await ed.process_char(ch)
        await ed.process_char("\x01")  # go to start
        writes.clear()
        await ed.process_char("a")
        assert ed.get_buffer() == "abc"
        assert ed.cursor_pos == 1
        # Emitted: "a" + "bc" (suffix redraw) + \x1b[2D (move back 2)
        combined = _joined(writes)
        assert "a" in combined
        assert "bc" in combined
        assert "\x1b[2D" in combined

    async def test_insert_in_middle(self) -> None:
        ed, _ = _editor()
        for ch in "ac":
            await ed.process_char(ch)
        await ed.process_char("\x01")  # start
        await ed.process_char("\x06")  # pos 1 (after 'a')
        await ed.process_char("b")
        assert ed.get_buffer() == "abc"
        assert ed.cursor_pos == 2

    async def test_insert_at_end_is_simple_echo(self) -> None:
        ed, writes = _editor()
        await ed.process_char("a")
        writes.clear()
        await ed.process_char("b")
        assert ed.get_buffer() == "ab"
        assert writes == ["b"]  # simple echo, no redraw


# ---------------------------------------------------------------------------
# Mid-line backspace
# ---------------------------------------------------------------------------


class TestMidlineBackspace:
    async def test_backspace_at_middle_removes_correct_char(self) -> None:
        ed, writes = _editor()
        for ch in "abc":
            await ed.process_char(ch)
        # Move to position 2 (after 'b')
        await ed.process_char("\x01")
        await ed.process_char("\x06")
        await ed.process_char("\x06")
        writes.clear()
        await ed.process_char("\x7f")  # delete 'b'
        assert ed.get_buffer() == "ac"
        assert ed.cursor_pos == 1
        combined = _joined(writes)
        assert "\x08" in combined  # moved left
        assert "c" in combined  # tail redrawn

    async def test_backspace_at_start_noop(self) -> None:
        ed, writes = _editor()
        await ed.process_char("x")
        await ed.process_char("\x01")  # go to start
        writes.clear()
        await ed.process_char("\x7f")
        assert ed.get_buffer() == "x"
        assert writes == []


# ---------------------------------------------------------------------------
# Regular characters + max_length
# ---------------------------------------------------------------------------


class TestRegularChars:
    async def test_char_added_to_buffer(self) -> None:
        ed, writes = _editor()
        result = await ed.process_char("a")
        assert result is None
        assert ed.get_buffer() == "a"
        assert "a" in writes

    async def test_max_length_enforced(self) -> None:
        ed, writes = _editor(max_length=2)
        await ed.process_char("a")
        await ed.process_char("b")
        await ed.process_char("c")  # should be ignored
        assert ed.get_buffer() == "ab"
        assert writes.count("c") == 0

    async def test_char_without_on_write(self) -> None:
        ed = LineEditor()
        await ed.process_char("q")
        assert ed.get_buffer() == "q"


# ---------------------------------------------------------------------------
# Password mode
# ---------------------------------------------------------------------------


class TestPasswordMode:
    async def test_password_mode_echoes_star(self) -> None:
        ed, writes = _editor(password_mode=True)
        await ed.process_char("s")
        await ed.process_char("e")
        assert ed.get_buffer() == "se"
        assert writes == ["*", "*"]

    async def test_normal_mode_echoes_char(self) -> None:
        ed, writes = _editor(password_mode=False)
        await ed.process_char("x")
        assert "x" in writes

    async def test_password_mode_backspace_masks_suffix(self) -> None:
        ed, _ = _editor(password_mode=True)
        for ch in "ab":
            await ed.process_char(ch)
        await ed.process_char("\x01")  # go to start
        await ed.process_char("x")  # insert at start
        assert ed.get_buffer() == "xab"
        assert ed.cursor_pos == 1


# ---------------------------------------------------------------------------
# reset / get_buffer / set_max_length / set_password_mode
# ---------------------------------------------------------------------------


class TestMutators:
    async def test_reset_clears_buffer(self) -> None:
        ed, _ = _editor()
        await ed.process_char("a")
        ed.reset()
        assert ed.get_buffer() == ""
        assert ed.cursor_pos == 0

    async def test_set_max_length(self) -> None:
        ed, _ = _editor(max_length=10)
        ed.set_max_length(2)
        await ed.process_char("a")
        await ed.process_char("b")
        await ed.process_char("c")
        assert ed.get_buffer() == "ab"

    async def test_set_password_mode_on(self) -> None:
        ed, writes = _editor()
        ed.set_password_mode(True)
        await ed.process_char("p")
        assert writes == ["*"]

    async def test_set_password_mode_off(self) -> None:
        ed, writes = _editor(password_mode=True)
        ed.set_password_mode(False)
        await ed.process_char("q")
        assert writes == ["q"]
