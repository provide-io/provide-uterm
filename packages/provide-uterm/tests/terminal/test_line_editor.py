#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for LineEditor — readline-style terminal line editor."""

from __future__ import annotations

from provide.terminal.line_editor import LineEditor

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


class TestEnter:
    async def test_cr_completes_line(self) -> None:
        ed, writes = _editor()
        await ed.process_char("h")
        await ed.process_char("i")
        result = await ed.process_char("\r")
        assert result == "hi"
        assert writes[-1] == "\r\n"

    async def test_lf_completes_line(self) -> None:
        ed, writes = _editor()
        await ed.process_char("x")
        result = await ed.process_char("\n")
        assert result == "x"
        assert writes[-1] == "\r\n"

    async def test_enter_resets_buffer(self) -> None:
        ed, _ = _editor()
        await ed.process_char("a")
        await ed.process_char("\r")
        result2 = await ed.process_char("b")
        assert result2 is None
        assert ed.get_buffer() == "b"

    async def test_enter_resets_cursor_pos(self) -> None:
        ed, _ = _editor()
        await ed.process_char("a")
        await ed.process_char("\r")
        assert ed.cursor_pos == 0

    async def test_enter_on_empty_buffer(self) -> None:
        ed, writes = _editor()
        result = await ed.process_char("\r")
        assert result == ""
        assert writes == ["\r\n"]

    async def test_enter_without_on_write(self) -> None:
        ed = LineEditor()
        await ed.process_char("z")
        result = await ed.process_char("\r")
        assert result == "z"


# ---------------------------------------------------------------------------
# Backspace / Delete (cursor at end)
# ---------------------------------------------------------------------------


class TestBackspace:
    async def test_del_removes_last_char(self) -> None:
        ed, writes = _editor()
        await ed.process_char("a")
        await ed.process_char("b")
        result = await ed.process_char("\x7f")
        assert result is None
        assert ed.get_buffer() == "a"
        # Sequence: \x08 (move left) + space (overwrite) + \x1b[1D (move back)
        combined = _joined(writes)
        assert "\x08" in combined
        assert " " in combined

    async def test_bs_removes_last_char(self) -> None:
        ed, writes = _editor()
        await ed.process_char("x")
        await ed.process_char("\x08")
        assert ed.get_buffer() == ""
        assert ed.cursor_pos == 0

    async def test_backspace_on_empty_buffer_is_noop(self) -> None:
        ed, writes = _editor()
        result = await ed.process_char("\x7f")
        assert result is None
        assert ed.get_buffer() == ""
        assert writes == []

    async def test_backspace_without_on_write(self) -> None:
        ed = LineEditor()
        await ed.process_char("a")
        await ed.process_char("\x7f")
        assert ed.get_buffer() == ""

    async def test_backspace_decrements_cursor_pos(self) -> None:
        ed, _ = _editor()
        await ed.process_char("a")
        await ed.process_char("b")
        assert ed.cursor_pos == 2
        await ed.process_char("\x7f")
        assert ed.cursor_pos == 1


# ---------------------------------------------------------------------------
# Cursor position tracking
# ---------------------------------------------------------------------------


class TestCursorPos:
    async def test_cursor_starts_at_zero(self) -> None:
        ed, _ = _editor()
        assert ed.cursor_pos == 0

    async def test_cursor_advances_on_insert(self) -> None:
        ed, _ = _editor()
        await ed.process_char("a")
        assert ed.cursor_pos == 1
        await ed.process_char("b")
        assert ed.cursor_pos == 2

    async def test_cursor_at_end_equals_buffer_len(self) -> None:
        ed, _ = _editor()
        for ch in "hello":
            await ed.process_char(ch)
        assert ed.cursor_pos == len(ed.buffer) == 5

    async def test_reset_clears_cursor_pos(self) -> None:
        ed, _ = _editor()
        await ed.process_char("a")
        ed.reset()
        assert ed.cursor_pos == 0
        assert ed.get_buffer() == ""


# ---------------------------------------------------------------------------
# Ctrl+A / Ctrl+E (cursor movement — relative ANSI)
# ---------------------------------------------------------------------------


class TestCtrlAE:
    async def test_ctrl_a_moves_to_start(self) -> None:
        ed, writes = _editor()
        await ed.process_char("a")
        result = await ed.process_char("\x01")
        assert result is None
        assert ed.cursor_pos == 0
        # Relative left move: \x1b[1D (move left 1 from position 1)
        combined = _joined(writes)
        assert "\x1b[1D" in combined

    async def test_ctrl_a_on_empty_buffer_noop(self) -> None:
        ed, writes = _editor()
        await ed.process_char("\x01")
        assert ed.cursor_pos == 0
        # Nothing sent for move when already at start
        assert not any("\x1b[" in w for w in writes)

    async def test_ctrl_a_when_already_at_start_noop(self) -> None:
        ed, writes = _editor()
        await ed.process_char("x")
        await ed.process_char("\x01")  # moves to start
        writes.clear()
        await ed.process_char("\x01")  # already at start — noop
        assert writes == []

    async def test_ctrl_e_moves_to_end(self) -> None:
        ed, writes = _editor()
        await ed.process_char("a")
        await ed.process_char("b")
        await ed.process_char("\x01")  # go to start
        writes.clear()
        await ed.process_char("\x05")  # Ctrl+E: back to end
        assert ed.cursor_pos == 2
        combined = _joined(writes)
        assert "\x1b[2C" in combined  # move right 2

    async def test_ctrl_e_at_end_noop(self) -> None:
        ed, writes = _editor()
        await ed.process_char("a")
        await ed.process_char("b")
        writes.clear()
        await ed.process_char("\x05")  # already at end
        assert writes == []
        assert ed.cursor_pos == 2

    async def test_ctrl_e_on_empty_buffer_noop(self) -> None:
        ed, writes = _editor()
        await ed.process_char("\x05")
        assert not any(w.startswith("\x1b[") for w in writes)

    async def test_ctrl_a_without_on_write(self) -> None:
        ed = LineEditor()
        await ed.process_char("x")
        result = await ed.process_char("\x01")
        assert result is None
        assert ed.cursor_pos == 0

    async def test_ctrl_a_e_roundtrip(self) -> None:
        ed, _ = _editor()
        for ch in "hello":
            await ed.process_char(ch)
        assert ed.cursor_pos == 5
        await ed.process_char("\x01")
        assert ed.cursor_pos == 0
        await ed.process_char("\x05")
        assert ed.cursor_pos == 5


# ---------------------------------------------------------------------------
# Ctrl+B / Ctrl+F (single-char left/right)
# ---------------------------------------------------------------------------


class TestCtrlBF:
    async def test_ctrl_b_moves_left(self) -> None:
        ed, writes = _editor()
        await ed.process_char("a")
        await ed.process_char("b")
        assert ed.cursor_pos == 2
        await ed.process_char("\x02")
        assert ed.cursor_pos == 1
        assert "\x1b[D" in _joined(writes)

    async def test_ctrl_b_at_start_noop(self) -> None:
        ed, writes = _editor()
        await ed.process_char("x")
        await ed.process_char("\x01")  # go to start
        writes.clear()
        await ed.process_char("\x02")
        assert ed.cursor_pos == 0
        assert writes == []

    async def test_ctrl_f_moves_right(self) -> None:
        ed, writes = _editor()
        await ed.process_char("a")
        await ed.process_char("b")
        await ed.process_char("\x01")  # go to start
        writes.clear()
        await ed.process_char("\x06")
        assert ed.cursor_pos == 1
        assert "\x1b[C" in _joined(writes)

    async def test_ctrl_f_at_end_noop(self) -> None:
        ed, writes = _editor()
        await ed.process_char("x")
        writes.clear()
        await ed.process_char("\x06")  # already at end
        assert ed.cursor_pos == 1
        assert writes == []

    async def test_ctrl_bf_step_through(self) -> None:
        ed, _ = _editor()
        for ch in "abc":
            await ed.process_char(ch)
        assert ed.cursor_pos == 3
        await ed.process_char("\x02")
        assert ed.cursor_pos == 2
        await ed.process_char("\x02")
        assert ed.cursor_pos == 1
        await ed.process_char("\x06")
        assert ed.cursor_pos == 2


# ---------------------------------------------------------------------------
# Ctrl+U / Ctrl+K (kill line segments)
# ---------------------------------------------------------------------------


class TestCtrlUK:
    async def test_ctrl_u_from_end_clears_buffer(self) -> None:
        ed, writes = _editor()
        await ed.process_char("h")
        await ed.process_char("i")
        result = await ed.process_char("\x15")
        assert result is None
        assert ed.get_buffer() == ""
        assert ed.cursor_pos == 0
        combined = _joined(writes)
        assert "\x1b[2D" in combined  # moved left 2
        assert "\x1b[K" in combined  # erased to EOL

    async def test_ctrl_u_from_middle_kills_backward(self) -> None:
        ed, writes = _editor()
        for ch in "abcd":
            await ed.process_char(ch)
        await ed.process_char("\x01")  # go to start
        await ed.process_char("\x06")  # move right to pos 1
        await ed.process_char("\x06")  # pos 2
        writes.clear()
        await ed.process_char("\x15")  # kill "ab", keep "cd"
        assert ed.get_buffer() == "cd"
        assert ed.cursor_pos == 0

    async def test_ctrl_u_on_empty_buffer_noop(self) -> None:
        ed, writes = _editor()
        await ed.process_char("\x15")
        assert writes == []

    async def test_ctrl_k_from_middle_kills_forward(self) -> None:
        ed, writes = _editor()
        for ch in "hello":
            await ed.process_char(ch)
        await ed.process_char("\x01")  # go to start
        await ed.process_char("\x06")  # move to pos 1
        await ed.process_char("\x06")  # pos 2
        writes.clear()
        await ed.process_char("\x0b")  # kill "llo"
        assert ed.get_buffer() == "he"
        assert ed.cursor_pos == 2
        assert "\x1b[K" in _joined(writes)

    async def test_ctrl_k_at_end_noop(self) -> None:
        ed, writes = _editor()
        await ed.process_char("x")
        writes.clear()
        await ed.process_char("\x0b")  # at end — nothing to kill
        assert ed.get_buffer() == "x"
        assert writes == []

    async def test_ctrl_k_on_empty_buffer_noop(self) -> None:
        ed, writes = _editor()
        await ed.process_char("\x0b")
        assert writes == []

    async def test_ctrl_u_without_on_write(self) -> None:
        ed = LineEditor()
        await ed.process_char("a")
        result = await ed.process_char("\x15")
        assert result is None
        assert ed.get_buffer() == ""

    async def test_ctrl_k_without_on_write(self) -> None:
        ed = LineEditor()
        await ed.process_char("a")
        await ed.process_char("\x01")  # go to start so ^K can kill forward
        result = await ed.process_char("\x0b")
        assert result is None
        assert ed.get_buffer() == ""


# ---------------------------------------------------------------------------
# Ctrl+W (kill word backward)
# ---------------------------------------------------------------------------


class TestCtrlW:
    async def test_ctrl_w_kills_word(self) -> None:
        ed, writes = _editor()
        for ch in "hello":
            await ed.process_char(ch)
        await ed.process_char("\x17")
        assert ed.get_buffer() == ""
        assert ed.cursor_pos == 0

    async def test_ctrl_w_kills_last_word(self) -> None:
        ed, _ = _editor()
        for ch in "hello world":
            await ed.process_char(ch)
        await ed.process_char("\x17")
        assert ed.get_buffer() == "hello "
        assert ed.cursor_pos == 6

    async def test_ctrl_w_skips_trailing_spaces(self) -> None:
        ed, _ = _editor()
        for ch in "hello   ":
            await ed.process_char(ch)
        await ed.process_char("\x17")
        assert ed.get_buffer() == ""

    async def test_ctrl_w_at_start_noop(self) -> None:
        ed, writes = _editor()
        for ch in "hi":
            await ed.process_char(ch)
        await ed.process_char("\x01")  # go to start
        writes.clear()
        await ed.process_char("\x17")
        assert ed.get_buffer() == "hi"
        assert writes == []

    async def test_ctrl_w_from_middle_with_remaining(self) -> None:
        # Kill word backward while cursor is in the middle (remaining chars after)
        ed, writes = _editor()
        for ch in "foo bar":
            await ed.process_char(ch)
        # Move cursor back to after "foo " (position 4)
        await ed.process_char("\x01")  # start
        for _ in range(4):
            await ed.process_char("\x06")  # right to pos 4
        writes.clear()
        await ed.process_char("\x17")  # kill "foo " (space is consumed by skip-spaces)
        assert ed.get_buffer() == "bar"
        assert ed.cursor_pos == 0
        # Suffix "bar" must have been redrawn and cursor moved back
        combined = _joined(writes)
        assert "bar" in combined
        assert "\x1b[" in combined  # move-back sequence emitted

    async def test_ctrl_w_on_empty_buffer_noop(self) -> None:
        ed, writes = _editor()
        await ed.process_char("\x17")
        assert writes == []


# ---------------------------------------------------------------------------
# Mid-line insert
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
