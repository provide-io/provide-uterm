#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the manager's timeseries reader.

A manager writes one JSON line per sample and reads the tail back to show an
operator what a fleet has been doing. Two decisions are worth pinning:

* **Where the current run starts.** A file outlives the fleet it describes,
  so a chart drawn over the whole file draws several runs at once. A run is
  taken to have restarted when the turn count drops sharply — by more than a
  fifth, and by at least fifty, so ordinary noise is not a restart — or when
  the agents go from some to none.
* **What a malformed line does.** Nothing: a truncated write at the end of a
  file, a blank line or a line that is not an object is skipped, because a
  reader that failed on one bad line would lose every good one behind it.

# uv-package: provide-uterm-platform

Usage (from the repository root)::

    uv run --package provide-uterm-platform python \\
        packages/provide-uterm-ts/testdata/gen_timeseries_golden.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from provide.uterm.manager import constants
from provide.uterm.manager.timeseries.manager import TimeseriesManager

OUT = Path(__file__).resolve().parent / "timeseries_golden.json"


def _row(turns: int, agents: int, tag: str = "") -> dict[str, Any]:
    return {"total_turns": turns, "total_agents": agents, "tag": tag}


EPOCHS: list[tuple[str, list[dict[str, Any]]]] = [
    ("nothing at all", []),
    ("one sample", [_row(10, 2)]),
    ("a steady run", [_row(10, 2), _row(20, 2), _row(30, 2)]),
    ("a run that grew", [_row(10, 1), _row(200, 4), _row(500, 8)]),
    ("a small dip", [_row(100, 2), _row(90, 2), _row(120, 2)]),
    ("a dip of exactly the floor", [_row(100, 2), _row(50, 2), _row(120, 2)]),
    ("a dip past the floor", [_row(100, 2), _row(49, 2), _row(120, 2)]),
    ("a big drop", [_row(1000, 4), _row(10, 1), _row(20, 1)]),
    ("a drop of exactly a fifth", [_row(1000, 4), _row(800, 4), _row(820, 4)]),
    ("a drop past a fifth", [_row(1000, 4), _row(700, 4), _row(720, 4)]),
    ("the agents going away", [_row(100, 4), _row(110, 0), _row(120, 2)]),
    ("the agents never there", [_row(100, 0), _row(110, 0), _row(120, 0)]),
    ("the agents arriving", [_row(100, 0), _row(110, 4), _row(120, 4)]),
    ("two restarts", [_row(1000, 4), _row(5, 1), _row(900, 4), _row(3, 1), _row(50, 2)]),
    ("a restart at the very end", [_row(1000, 4), _row(1100, 4), _row(2, 1)]),
    ("a row with nothing in it", [{}, _row(10, 2)]),
    ("a row whose counts are null", [{"total_turns": None, "total_agents": None}, _row(10, 2)]),
    ("counts given as text", [{"total_turns": "100", "total_agents": "4"}, _row(10, 1)]),
]

TAILS: list[tuple[str, str, int]] = [
    ("nothing at all", "", 10),
    ("one row", '{"a":1}\n', 10),
    ("several rows", '{"a":1}\n{"a":2}\n{"a":3}\n', 10),
    ("more rows than asked for", "".join(f'{{"a":{n}}}\n' for n in range(10)), 3),
    ("no trailing newline", '{"a":1}\n{"a":2}', 10),
    ("a blank line", '{"a":1}\n\n{"a":2}\n', 10),
    ("a line of spaces", '{"a":1}\n   \n{"a":2}\n', 10),
    ("a truncated last line", '{"a":1}\n{"a":2', 10),
    ("a line that is not json", '{"a":1}\nnot json\n{"a":2}\n', 10),
    ("a line that is a list", '{"a":1}\n[1,2]\n{"a":2}\n', 10),
    ("a line that is a number", '{"a":1}\n42\n{"a":2}\n', 10),
    ("a line that is a string", '{"a":1}\n"hello"\n{"a":2}\n', 10),
    ("a line that is null", '{"a":1}\nnull\n{"a":2}\n', 10),
    ("a limit of zero", '{"a":1}\n{"a":2}\n', 0),
    ("a negative limit", '{"a":1}\n{"a":2}\n', -5),
    ("text outside ASCII", '{"a":"h\\u00e9llo"}\n', 10),
]


def _manager(directory: str) -> TimeseriesManager:
    """A manager over a scratch directory. It picks its own file name."""
    return TimeseriesManager(dict, timeseries_dir=directory)


def _tail(contents: str, limit: int) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory() as directory:
        manager = _manager(directory)
        manager.path.write_text(contents, encoding="utf-8")
        return manager.read_tail(limit)


def _missing_file() -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory() as directory:
        # The manager names a file it has not written yet.
        return _manager(directory).read_tail(10)


def main() -> None:
    corpus = {
        "constants": {
            "epoch_turn_drop_ratio": constants.EPOCH_TURN_DROP_RATIO,
            "epoch_turn_drop_min": constants.EPOCH_TURN_DROP_MIN,
        },
        "epochs": [
            {"name": name, "rows": rows, "trimmed": TimeseriesManager.trim_to_latest_epoch(list(rows))}
            for name, rows in EPOCHS
        ],
        "tails": [
            {"name": name, "contents": contents, "limit": limit, "rows": _tail(contents, limit)}
            for name, contents, limit in TAILS
        ],
        "missing_file": _missing_file(),
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['epochs'])} epoch cases)")


if __name__ == "__main__":
    main()
