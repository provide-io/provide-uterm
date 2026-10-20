#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A capture session's scrollback, and the screen it is given back after a restart.

The server keeps a PAM capture session's last screen in a file. Re-created
after a restart, its new connector must show that screen until the program
draws again, and must keep the rows that had scrolled off above it.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from provide.uterm import strip_ansi
from provide.uterm.pty.capture_connector import CaptureConnector


def _connector(**config: object) -> CaptureConnector:
    return CaptureConnector("s", "d", {"socket_path": "/tmp/cap.sock", **config})


def _rows(connector: CaptureConnector) -> list[str]:
    return strip_ansi(connector._snapshot()["screen"]).split("\n")


def test_no_scrollback_is_kept_until_asked_for() -> None:
    connector = _connector(cols=10, rows=2)
    connector._ingest_stdout(b"a\nb\nc\n")
    assert connector.scrollback() == []


def test_scrollback_keeps_the_newest_rows_up_to_its_bound() -> None:
    connector = _connector(cols=10, rows=2)
    connector.keep_scrollback(2)
    connector._ingest_stdout(b"one\ntwo\nthree\nfour\nfive")
    assert connector.scrollback() == ["two", "three"]


def test_scrollback_kept_before_this_connector_started_is_carried_on_within_its_bound() -> None:
    connector = _connector(cols=10, rows=1)
    connector.keep_scrollback(2, ["older", "old", "last"])
    assert connector.scrollback() == ["old", "last"]
    connector._ingest_stdout(b"a\nb")
    assert connector.scrollback() == ["last", "a"]


def test_no_scrollback_renders_no_row() -> None:
    connector = _connector(cols=10, rows=1)
    connector.keep_scrollback(0)
    with patch.object(type(connector._emulator._screen), "row_text") as row_text:
        connector._ingest_stdout(b"a\nb\nc")
    row_text.assert_not_called()
    assert connector.scrollback() == []


def test_a_resize_does_not_scroll_the_replay_into_the_scrollback_again() -> None:
    connector = _connector(cols=10, rows=2)
    connector.keep_scrollback(10)
    connector._ingest_stdout(b"one\ntwo\nthree")
    assert connector.reconfigure({"socket_path": "/tmp/cap.sock", "cols": 12, "rows": 2})
    assert connector.scrollback() == ["one"]
    connector._ingest_stdout(b"\nfour")
    assert connector.scrollback() == ["one", "two"]


async def test_clear_empties_the_scrollback() -> None:
    connector = _connector(cols=10, rows=1)
    connector.keep_scrollback(10)
    connector._ingest_stdout(b"a\nb")
    await connector.clear()
    assert connector.scrollback() == []


def test_a_restored_screen_paints_back_exactly_with_its_size_and_scrollback() -> None:
    live = _connector(cols=20, rows=4)
    live._ingest_stdout(b"\x1b[1;31mred\x1b[0m line\n\x1b[2;5Hmoved\x1b[?25l")
    saved = live._snapshot()

    restored = _connector()
    restored.keep_scrollback(10, ["older", "old"])
    restored.restore_screen(saved["screen"], 20, 4)
    again = restored._snapshot()
    assert (again["cols"], again["rows"]) == (20, 4)
    assert again["screen"] == saved["screen"]
    assert again["cursor"] == saved["cursor"]
    assert restored.scrollback() == ["older", "old"]


def test_output_after_a_restore_lands_where_the_program_left_its_cursor() -> None:
    live = _connector(cols=20, rows=3)
    live._ingest_stdout(b"$ ls\nfile\n$ ")
    restored = _connector()
    restored.restore_screen(live._snapshot()["screen"], 20, 3)
    restored._ingest_stdout(b"pwd")
    live._ingest_stdout(b"pwd")
    assert _rows(restored) == _rows(live)


def test_a_resize_after_a_restore_redraws_the_restored_screen() -> None:
    live = _connector(cols=20, rows=3)
    live._ingest_stdout(b"kept on screen")
    restored = _connector()
    restored.restore_screen(live._snapshot()["screen"], 20, 3)
    assert restored.reconfigure({"socket_path": "/tmp/cap.sock", "cols": 30, "rows": 3})
    assert _rows(restored)[0].startswith("kept on screen")


def test_rows_scrolled_off_after_a_restore_join_its_scrollback() -> None:
    restored = _connector()
    restored.keep_scrollback(10, ["earlier"])
    restored.restore_screen("top\nmiddle\nbottom", 10, 3)
    restored._ingest_stdout(b"\nnext")
    assert restored.scrollback() == ["earlier", "top"]


def test_a_restore_without_the_emulator_keeps_the_screen_as_the_raw_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "provide.uterm.emulator", None)
    restored = _connector()
    restored.keep_scrollback(10, ["x"])
    restored.restore_screen("a\nb", 10, 2)
    assert restored._snapshot()["screen"] == "a\r\nb"
    assert restored.scrollback() == ["x"]
