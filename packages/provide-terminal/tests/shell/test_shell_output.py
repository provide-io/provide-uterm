#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.shell._output — ANSI helpers and frame builders."""

import time

from provide.terminal.shell._output import (
    BANNER,
    BLUE,
    BOLD,
    CLEAR_SCREEN,
    CYAN,
    DIM,
    GREEN,
    MAGENTA,
    PROMPT,
    RED,
    RESET,
    YELLOW,
    error_msg,
    fmt_kv,
    fmt_table,
    heading,
    info_msg,
    success_msg,
)
from provide.terminal.shell.terminal._output import term, worker_hello


def test_term_with_ts():
    frame = term("hello", ts=1234.5)
    assert frame == {"type": "term", "data": "hello", "ts": 1234.5}


def test_term_without_ts():
    before = time.time()
    frame = term("hi")
    after = time.time()
    assert frame["type"] == "term"
    assert frame["data"] == "hi"
    assert before <= frame["ts"] <= after


def test_worker_hello_default():
    frame = worker_hello()
    assert frame["type"] == "worker_hello"
    assert frame["input_mode"] == "open"
    assert "ts" in frame


def test_worker_hello_custom_mode():
    frame = worker_hello("hijack")
    assert frame["input_mode"] == "hijack"


def test_error_msg():
    msg = error_msg("bad thing")
    assert "bad thing" in msg
    assert RED in msg
    assert RESET in msg
    assert msg.endswith("\r\n")


def test_info_msg():
    msg = info_msg("some info")
    assert "some info" in msg
    assert DIM in msg
    assert msg.endswith("\r\n")


def test_success_msg():
    msg = success_msg("done")
    assert "done" in msg
    assert GREEN in msg
    assert msg.endswith("\r\n")


def test_heading():
    msg = heading("My Title")
    assert "My Title" in msg
    assert BOLD in msg
    assert CYAN in msg
    assert msg.endswith("\r\n")


def test_fmt_kv_default_width():
    msg = fmt_kv("key", "val")
    assert "key" in msg
    assert "val" in msg
    assert msg.endswith("\r\n")


def test_fmt_kv_custom_width():
    msg = fmt_kv("k", "v", width=5)
    assert "k" in msg
    assert "v" in msg


def test_fmt_table_empty():
    result = fmt_table([])
    assert "(no results)" in result


def test_fmt_table_no_headers():
    rows = [("a", "b"), ("cc", "dd")]
    result = fmt_table(rows)
    assert "a" in result
    assert "cc" in result
    assert "\r\n" in result


def test_fmt_table_with_headers():
    rows = [("alice", "admin"), ("bob", "viewer")]
    result = fmt_table(rows, headers=("name", "role"))
    assert "name" in result
    assert "role" in result
    assert "alice" in result
    assert "bob" in result
    # header separator line
    assert "-" in result


def test_fmt_table_headers_wider_than_data():
    # headers wider than data forces widths expansion
    rows = [("a", "b")]
    result = fmt_table(rows, headers=("longerheader", "anotherlong"))
    assert "longerheader" in result


def test_constants_defined():
    for c in (RESET, BOLD, DIM, GREEN, YELLOW, RED, CYAN, BLUE, MAGENTA, CLEAR_SCREEN, BANNER, PROMPT):
        assert isinstance(c, str)
        assert len(c) > 0


def test_import_init():
    from provide.terminal.shell.terminal import UshellConnector

    assert UshellConnector is not None
