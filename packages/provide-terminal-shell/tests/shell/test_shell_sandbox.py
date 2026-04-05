#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.shell._sandbox — Sandbox eval/exec."""

from provide.terminal.shell._sandbox import Sandbox


def test_eval_returns_result():
    sb = Sandbox()
    out = sb.run("1 + 1")
    assert "2" in out


def test_eval_returns_none_no_output():
    sb = Sandbox()
    out = sb.run("None")
    # eval of None → result is None → result_str stays ""
    assert out == ""


def test_exec_statement_no_output():
    sb = Sandbox()
    out = sb.run("x = 42")
    # assignment is a statement; no printed output
    assert out == ""


def test_persistent_namespace():
    sb = Sandbox()
    sb.run("x = 10")
    out = sb.run("x * 2")
    assert "20" in out


def test_print_capture():
    sb = Sandbox()
    out = sb.run("print('hello')")
    assert "hello" in out


def test_print_capture_newline_replaced():
    sb = Sandbox()
    out = sb.run("print('a\\nb')")
    # newlines become \r\n
    assert "\r\n" in out


def test_eval_exception():
    sb = Sandbox()
    out = sb.run("1/0")
    assert "ZeroDivisionError" in out
    # output has ANSI red color prefix
    assert "\x1b[31m" in out


def test_exec_exception():
    sb = Sandbox()
    out = sb.run("raise ValueError('oops')")
    assert "ValueError" in out
    assert "oops" in out


def test_extra_injected_into_namespace():
    sb = Sandbox(extra={"MY_VAR": 99})
    out = sb.run("MY_VAR")
    assert "99" in out


def test_print_with_sep_end():
    sb = Sandbox()
    out = sb.run("print('a', 'b', sep='-', end='!')")
    assert "a-b!" in out


def test_print_multiple_calls():
    sb = Sandbox()
    out = sb.run("print('first')\nprint('second')")
    assert "first" in out
    assert "second" in out


def test_output_clears_between_runs():
    sb = Sandbox()
    sb.run("print('first run')")
    out = sb.run("print('second run')")
    assert "first run" not in out
    assert "second run" in out


def test_sandbox_no_extra():
    sb = Sandbox()
    # __builtins__ should be restricted
    assert "print" in sb.namespace


def test_safe_builtins_available():
    sb = Sandbox()
    out = sb.run("len([1,2,3])")
    assert "3" in out


def test_unsafe_builtin_not_available():
    sb = Sandbox()
    out = sb.run("open('test')")
    # open not in builtins → NameError
    assert "NameError" in out or "open" in out


# ---------------------------------------------------------------------------
# Pre-imported safe modules
# ---------------------------------------------------------------------------


def test_json_available():
    sb = Sandbox()
    out = sb.run("json.dumps({'x': 1})")
    assert '"x"' in out


def test_datetime_available():
    sb = Sandbox()
    out = sb.run("datetime.date(2025, 1, 1).year")
    assert "2025" in out


def test_re_available():
    sb = Sandbox()
    out = sb.run(r"re.findall(r'\d+', 'a1b2')")
    assert "['1', '2']" in out


def test_hashlib_available():
    sb = Sandbox()
    out = sb.run("hashlib.md5(b'hello').hexdigest()")
    assert "5d41402abc4b" in out


def test_base64_available():
    sb = Sandbox()
    out = sb.run("base64.b64encode(b'hi').decode()")
    assert "aGk=" in out
