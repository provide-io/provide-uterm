#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.shell._repl — LineBuffer keystroke handling."""

from provide.uterm.shell._repl import LineBuffer


def test_printable_chars_echoed():
    buf = LineBuffer()
    buf.feed("hi")
    assert buf.take_echo() == "hi"
    assert buf.take_completed() == []


def test_tab_treated_as_printable():
    buf = LineBuffer()
    buf.feed("\t")
    assert buf.take_echo() == "\t"
    assert buf.current_line() == "\t"


def test_enter_cr_completes_line():
    buf = LineBuffer()
    buf.feed("hello\r")
    echo = buf.take_echo()
    assert "\r\n" in echo
    lines = buf.take_completed()
    assert lines == ["hello"]


def test_enter_crlf_skips_lf():
    buf = LineBuffer()
    buf.feed("abc\r\n")
    echo = buf.take_echo()
    assert echo.count("\r\n") == 1
    assert buf.take_completed() == ["abc"]


def test_enter_lf_completes_line():
    buf = LineBuffer()
    buf.feed("line\n")
    assert buf.take_completed() == ["line"]


def test_backspace_del():
    buf = LineBuffer()
    buf.feed("ab\x7f")
    assert buf.current_line() == "a"
    echo = buf.take_echo()
    assert "\x08 \x08" in echo


def test_backspace_bs():
    buf = LineBuffer()
    buf.feed("xy\x08")
    assert buf.current_line() == "x"
    echo = buf.take_echo()
    assert "\x08 \x08" in echo


def test_backspace_on_empty_no_echo():
    buf = LineBuffer()
    buf.feed("\x7f")
    assert buf.take_echo() == ""
    assert buf.current_line() == ""


def test_ctrl_c_clears_buf_and_completes():
    buf = LineBuffer()
    buf.feed("partial\x03")
    lines = buf.take_completed()
    assert lines == ["\x03"]
    assert buf.current_line() == ""
    echo = buf.take_echo()
    assert "^C" in echo


def test_ctrl_d_with_buffer_content():
    buf = LineBuffer()
    buf.feed("hello\x04")
    lines = buf.take_completed()
    assert lines == ["hello"]
    echo = buf.take_echo()
    assert "\r\n" in echo


def test_ctrl_d_empty_buffer():
    buf = LineBuffer()
    buf.feed("\x04")
    lines = buf.take_completed()
    assert lines == ["\x04"]


def test_vt_csi_sequence_swallowed():
    # Arrow up: \x1b[A
    buf = LineBuffer()
    buf.feed("\x1b[A")
    assert buf.take_echo() == ""
    assert buf.current_line() == ""


def test_vt_csi_with_params_swallowed():
    # \x1b[1;2A — with params
    buf = LineBuffer()
    buf.feed("\x1b[1;2A")
    assert buf.take_echo() == ""


def test_ss3_sequence_swallowed():
    # \x1b O A — SS3 (F1 on some terminals)
    buf = LineBuffer()
    buf.feed("\x1bOA")
    assert buf.take_echo() == ""
    assert buf.current_line() == ""


def test_esc_at_end_of_string():
    # \x1b with nothing after — just advances past it
    buf = LineBuffer()
    buf.feed("\x1b")
    assert buf.take_echo() == ""


def test_esc_bracket_at_end_no_final_byte():
    # \x1b[ with no final byte — consumed without crash
    buf = LineBuffer()
    buf.feed("\x1b[")
    assert buf.take_echo() == ""


def test_esc_o_at_end_of_string():
    # \x1bO with no byte after O
    buf = LineBuffer()
    buf.feed("\x1bO")
    assert buf.take_echo() == ""


def test_other_control_bytes_ignored():
    # \x01 (Ctrl+A) is not a known control — silently dropped
    buf = LineBuffer()
    buf.feed("\x01")
    assert buf.take_echo() == ""
    assert buf.current_line() == ""


def test_max_line_limit():
    buf = LineBuffer(max_line=3)
    buf.feed("abcde")
    # Only first 3 chars accepted
    assert buf.current_line() == "abc"
    echo = buf.take_echo()
    assert echo == "abc"


def test_take_echo_drains():
    buf = LineBuffer()
    buf.feed("hi")
    _ = buf.take_echo()
    assert buf.take_echo() == ""


def test_take_completed_drains():
    buf = LineBuffer()
    buf.feed("cmd\r")
    _ = buf.take_completed()
    assert buf.take_completed() == []


def test_current_line():
    buf = LineBuffer()
    buf.feed("partial")
    assert buf.current_line() == "partial"


def test_clear_discards_buf():
    buf = LineBuffer()
    buf.feed("something")
    buf.clear()
    assert buf.current_line() == ""
    assert buf.take_echo() == ""


def test_empty_line_on_just_enter():
    buf = LineBuffer()
    buf.feed("\r")
    assert buf.take_completed() == [""]


def test_csi_no_final_byte_with_param_bytes():
    # \x1b[ followed by param bytes (0x30-0x3F) but no final byte
    buf = LineBuffer()
    buf.feed("\x1b[12")
    assert buf.take_echo() == ""
