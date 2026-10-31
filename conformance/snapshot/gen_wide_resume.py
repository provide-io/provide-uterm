#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate ``wide_resume.json``, the wide-character snapshot-resume corpus.

Each record is a stream, the size of the screen it is drawn on, and the
snapshot (``ansi_screen()``) the Python emulator takes of it in a UTF-8
session. Every port renders the same bytes for it, and a viewer painted from
the snapshot of any prefix of the stream and fed the rest ends in the same
state as one fed the whole stream (see each port's snapshot-resume test).

The streams cover wide characters (and the second cell each leaves), the last
column and the deferred wrap, the halves of a pair overwritten or deleted, pens,
and combining marks joined to a second cell, which is what a snapshot has to
recreate exactly for a session to be restored by replaying it.

Generated once and committed; regenerate with::

    uv run python conformance/snapshot/gen_wide_resume.py
"""

from __future__ import annotations

import json
from pathlib import Path

from provide.uterm.emulator import TerminalEmulator

OUT = Path(__file__).with_name("wide_resume.json")
COLS = 10
ROWS = 3

#: (name, stream), each drawn on a COLS x ROWS screen.
STREAMS: list[tuple[str, str]] = [
    ("wide characters mid row", "中a\U0001f600b"),
    ("a wide character in the last column", "x" * 9 + "中yz"),
    ("a wide character filling the row", "x" * 8 + "中yz"),
    ("styled wide characters", "\x1b[1;31m中\x1b[44m中\x1b[0ma"),
    ("the second half overwritten", "中中\x1b[1;2Ha\x1b[1;4H\x1b[32mb\x1b[0mc"),
    ("the second half overwritten at the deferred wrap", "x" * 8 + "中\x1b[1;10Hb"),
    ("a wide character over the second half of another", "中\x1b[1;2H中q"),
    ("the first half overwritten", "中中\x1b[1;3Hx"),
    ("the first half overwritten in the last column", "x" * 8 + "中\x1b[1;9Ha"),
    ("a styled first half overwritten", "\x1b[44m中\x1b[0m\x1b[1;1Hz"),
    ("the first half deleted", "中ab\x1b[1;1H\x1b[P"),
    ("marks joined to the second half", "中́x"),
    ("marks joined to a second half in the last column", "x" * 8 + "中́̂"),
    ("marks on a second half whose first half is gone", "中́\x1b[1;1Hz"),
    ("marks on such a second half in the last column", "x" * 8 + "中́\x1b[1;9Ha"),
    ("a mark joined to the row above", "abc\r\ńx"),
    ("wide characters wrapping down the screen", "\x1b[7m" + "中" * 13 + "\x1b[0m!"),
    ("marks on a second half at the deferred wrap, autowrap off", "\x1b[?7l" + "x" * 8 + "\u4e2d\u0301"),
    ("the same, autowrap back on", "\x1b[?7l" + "x" * 8 + "\u4e2d\u0301\x1b[?7h"),
    ("marks on a narrow cell at the deferred wrap, autowrap off", "\x1b[?7l" + "x" * 9 + "q\u0307"),
    ("the same narrow cell, autowrap back on", "\x1b[?7l" + "x" * 9 + "q\u0307\x1b[?7h"),
    ("marks joined to a wide character clipped at the last column", "x" * 9 + "\u4e2d\u0301"),
    ("a wide character with marks of its own", "\u4e2d\x1b[1;2H\u0301x"),
    ("a wide character with marks of its own and in its second half", "\u4e2d\u0302\x1b[1;2H\u0301"),
    ("marks of its own in the last two columns", "x" * 8 + "\u4e2d\u0302\x1b[1;10H\u0301\x1b[2;1H"),
    # A cursor deferred past the last column, moved onto another row by CUU,
    # CUD, IND or RI, which keep the column. Over these last cells it cannot be
    # left deferred by redrawing them, so it is left deferred on the next row
    # and moved back.
    (
        "deferred over a wide character with marks of its own",
        "x" * 8 + "\u4e2d\x1b[1;10H\u0301\x1b[2;1H" + "y" * 10 + "\x1b[A",
    ),
    ("deferred over a lost first half", "x" * 8 + "\u4e2d\x1b[1;9Ha\x1b[2;1H" + "y" * 10 + "\x1b[A"),
    ("deferred over a lost first half with marks", "x" * 8 + "\u4e2d\u0301\x1b[1;9Ha\x1b[2;1H" + "y" * 10 + "\x1b[A"),
    ("deferred over a lost first half from the row above", "x" * 8 + "\u4e2d\x1b[2;9Ha\x1b[1;1H" + "z" * 10 + "\x1b[B"),
    ("deferred by an index", "x" * 8 + "\u4e2d\x1b[2;9Ha\x1b[1;1H" + "z" * 10 + "\x1bD"),
    (
        "deferred in origin mode at the top of the region",
        "\x1b[2;3r\x1b[?6h" + "x" * 8 + "\u4e2d\x1b[1;9Ha\x1b[2;1H" + "y" * 10 + "\x1b[A",
    ),
    (
        "deferred where the row above cannot be drawn either",
        "x" * 8 + "\u4e2d\x1b[1;9Ha\x1b[2;1H" + "x" * 8 + "\u4e2d\x1b[2;9Hb\x1b[3;1H" + "y" * 10 + "\x1b[A",
    ),
    ("deferred by a reverse index", "x" * 8 + "\u4e2d\x1b[1;9Ha\x1b[2;1H" + "y" * 10 + "\x1bM"),
]


#: (name, stream), each drawn on a COLS x TALL_ROWS screen: a scrolling region
#: with rows outside it on both sides.
TALL_ROWS = 4
TALL_STREAMS: list[tuple[str, str]] = [
    # Below the region's bottom margin a CUD from the row above would stop at
    # the margin, so the row below is used.
    (
        "deferred below the region, where the row above is no use",
        "\x1b[1;2r\x1b[3;1H" + "x" * 8 + "\u4e2d\x1b[3;9Ha\x1b[2;1H" + "w" * 10 + "\x1b[4;1H" + "y" * 10 + "\x1b[A",
    ),
    (
        "deferred in origin mode at the bottom of the region",
        "\x1b[2;3r\x1b[?6h\x1b[2;1H" + "x" * 8 + "\u4e2d\x1b[2;9Ha\x1b[1;1H" + "z" * 10 + "\x1b[B",
    ),
]


def main() -> int:
    """Write the corpus and report the record count."""
    records = []
    for rows, streams in ((ROWS, STREAMS), (TALL_ROWS, TALL_STREAMS)):
        for name, stream in streams:
            emulator = TerminalEmulator(COLS, rows, receive_encoding="utf-8")
            emulator.process(stream.encode("utf-8"))
            records.append(
                {"name": name, "cols": COLS, "rows": rows, "stream": stream, "ansi_screen": emulator.ansi_screen()}
            )
    payload = {"generator": "conformance/snapshot/gen_wide_resume.py", "records": records}
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(records)} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
