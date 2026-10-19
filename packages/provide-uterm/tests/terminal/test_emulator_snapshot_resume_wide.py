#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A snapshot of wide characters resumes exactly, as every port takes it.

``conformance/snapshot/wide_resume.json`` holds each stream and the snapshot
this emulator takes of it; the Go, C# and TypeScript ports render the same
bytes. Every prefix of each stream, cut between characters and outside escape
sequences, is snapshotted, painted into a fresh emulator and fed the rest: the
screen, the cursor and the snapshot after that equal those of the whole stream.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from provide.uterm.emulator import TerminalEmulator
from provide.uterm.render.buffer import render_cell_rows

_CORPUS = Path(__file__).resolve().parents[4] / "conformance" / "snapshot" / "wide_resume.json"
_RECORDS: list[dict[str, Any]] = json.loads(_CORPUS.read_text(encoding="utf-8"))["records"]

#: One terminal token, as the byte-level splitter in test_emulator_snapshot_resume
#: has it, but over characters: a snapshot is never taken inside a character.
#: a, then a wide character whose first half is overwritten, shifted right by
#: an insert, and again: two empty second halves in the last two columns.
TWO_LOST_FIRST_HALVES = "a\u4e2d\x1b[1;2Hb\x1b[1;3H\x1b[@\x1b[1;2H\u4e2d\x1b[1;2Hc"

_TOKEN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b.|.", re.DOTALL)


def _emulator(record: dict[str, Any], stream: str = "") -> TerminalEmulator:
    emulator = TerminalEmulator(record["cols"], record["rows"], receive_encoding="utf-8")
    emulator.process(stream.encode("utf-8"))
    return emulator


def _state(emulator: TerminalEmulator) -> tuple[str, dict[str, int], str]:
    snapshot = emulator.get_snapshot()
    return snapshot["screen"], snapshot["cursor"], emulator.ansi_screen()


@pytest.mark.parametrize("record", _RECORDS, ids=[record["name"] for record in _RECORDS])
def test_the_snapshot_is_the_recorded_one(record: dict[str, Any]) -> None:
    assert _emulator(record, record["stream"]).ansi_screen() == record["ansi_screen"]


@pytest.mark.parametrize("record", _RECORDS, ids=[record["name"] for record in _RECORDS])
def test_a_snapshot_at_every_point_resumes_exactly(record: dict[str, Any]) -> None:
    stream = record["stream"]
    expected = _state(_emulator(record, stream))
    for split in [0, *(match.end() for match in _TOKEN.finditer(stream))]:
        viewer = _emulator(record, _emulator(record, stream[:split]).ansi_screen().replace("\n", "\r\n"))
        viewer.process(stream[split:].encode("utf-8"))
        assert _state(viewer) == expected, f"split at character {split}"


def test_a_wide_character_is_drawn_once_with_nothing_for_its_second_cell() -> None:
    emulator = _emulator({"cols": 6, "rows": 1}, "中a")

    assert render_cell_rows(emulator._screen.buffer, 6, 1) == ["\x1b[0m中a   \x1b[0m"]


def test_two_lost_first_halves_in_the_last_two_columns_are_left_blank() -> None:
    # Two second halves with their first halves gone, in the last two columns
    # (an insert shifted one there): the first is recreated, and the second,
    # with no cell left of it to recreate it from, is left blank.
    emulator = _emulator({"cols": 4, "rows": 1}, TWO_LOST_FIRST_HALVES)

    assert render_cell_rows(emulator._screen.buffer, 4, 1) == ["\x1b[0mac\u3000\x1b[3G\x1b[P\x1b[C\x1b[0m"]


def test_a_lost_first_half_in_a_one_column_row_is_left_blank() -> None:
    emulator = _emulator({"cols": 2, "rows": 1}, "中\x1b[1;1H\x1b[P")

    assert render_cell_rows(emulator._screen.buffer, 1, 1) == ["\x1b[0m"]


def test_an_empty_cell_under_a_deferred_cursor_is_drawn_as_a_blank() -> None:
    # Two empty second halves left under a cursor deferred past the last column
    # (a resize does it): no cell left of them and no neighbouring row to defer
    # it on, so the cell is drawn as a blank. On one column, and on two.
    one = _emulator({"cols": 2, "rows": 1}, "\u4e2d\x1b[1;1H\x1b[P\x1b[1;2H")
    one.resize(1, 1)
    two = _emulator({"cols": 4, "rows": 1}, "\u4e2d\u4e2d\x1b[1;1H\x1b[P\x1b[1;2H\x1b[P\x1b[1;3H")
    two.resize(2, 1)

    assert "\x1b[1;1H\x1b[0m \x1b[20l" in one.ansi_screen()
    assert "\x1b[1;1H\x1b[0m \x1b[20l" in two.ansi_screen()
