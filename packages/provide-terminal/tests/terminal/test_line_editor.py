#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
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
# Backspace / Delete
# ---------------------------------------------------------------------------


class TestBackspace:
    async def test_del_removes_last_char(self) -> None:
        ed, writes = _editor()
        await ed.process_char("a")
        await ed.process_char("b")
        result = await ed.process_char("\x7f")
        assert result is None
        assert ed.get_buffer() == "a"
        assert "\x08 \x08" in writes

    async def test_bs_removes_last_char(self) -> None:
        ed, writes = _editor()
        await ed.process_char("x")
        await ed.process_char("\x08")
        assert ed.get_buffer() == ""

    async def test_backspace_on_empty_buffer_is_noop(self) -> None:
        ed, writes = _editor()
        result = await ed.process_char("\x7f")
        assert result is None
        assert ed.get_buffer() == ""
        # no erase sequence sent
        assert "\x08 \x08" not in writes

    async def test_backspace_without_on_write(self) -> None:
        ed = LineEditor()
        await ed.process_char("a")
        await ed.process_char("\x7f")
        assert ed.get_buffer() == ""


# ---------------------------------------------------------------------------
# Ctrl+A / Ctrl+E (cursor movement)
# ---------------------------------------------------------------------------


class TestCtrlAE:
    async def test_ctrl_a_sends_home(self) -> None:
        ed, writes = _editor()
        await ed.process_char("a")
        result = await ed.process_char("\x01")
        assert result is None
        assert "\x1b[H" in writes

    async def test_ctrl_a_on_empty_buffer_noop(self) -> None:
        ed, writes = _editor()
        await ed.process_char("\x01")
        assert "\x1b[H" not in writes

    async def test_ctrl_e_sends_col_position(self) -> None:
        ed, writes = _editor()
        await ed.process_char("a")
        await ed.process_char("b")
        await ed.process_char("\x05")
        assert "\x1b[2G" in writes

    async def test_ctrl_e_on_empty_buffer_noop(self) -> None:
        ed, writes = _editor()
        await ed.process_char("\x05")
        # no escape sequence sent
        assert not any(w.startswith("\x1b[") for w in writes)

    async def test_ctrl_a_without_on_write(self) -> None:
        ed = LineEditor()
        await ed.process_char("x")
        result = await ed.process_char("\x01")
        assert result is None


# ---------------------------------------------------------------------------
# Ctrl+U / Ctrl+K (clear line)
# ---------------------------------------------------------------------------


class TestCtrlUK:
    async def test_ctrl_u_clears_buffer(self) -> None:
        ed, writes = _editor()
        await ed.process_char("h")
        await ed.process_char("i")
        result = await ed.process_char("\x15")
        assert result is None
        assert ed.get_buffer() == ""
        assert "\x1b[2K\r" in writes

    async def test_ctrl_u_on_empty_buffer_noop(self) -> None:
        ed, writes = _editor()
        await ed.process_char("\x15")
        assert "\x1b[2K\r" not in writes

    async def test_ctrl_k_clears_buffer(self) -> None:
        ed, writes = _editor()
        await ed.process_char("x")
        result = await ed.process_char("\x0b")
        assert result is None
        assert ed.get_buffer() == ""
        assert "\x1b[K" in writes

    async def test_ctrl_k_on_empty_buffer_noop(self) -> None:
        ed, writes = _editor()
        await ed.process_char("\x0b")
        assert "\x1b[K" not in writes

    async def test_ctrl_u_without_on_write(self) -> None:
        # Ctrl+U only clears when on_write is set (clear requires output)
        ed = LineEditor()
        await ed.process_char("a")
        result = await ed.process_char("\x15")
        assert result is None  # doesn't complete a line


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


# ---------------------------------------------------------------------------
# reset / get_buffer / set_max_length / set_password_mode
# ---------------------------------------------------------------------------


class TestMutators:
    async def test_reset_clears_buffer(self) -> None:
        ed, _ = _editor()
        await ed.process_char("a")
        ed.reset()
        assert ed.get_buffer() == ""

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
