#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for DeckMux names and edge bars.

Two small modules that both have to agree across ports, for different reasons.

**Names and colours are derived, not stored.** A connection gets its display
name and colour from a SHA-256 of its id, so two servers — or a server and a
reconnecting browser — arrive at the same answer without coordinating. That
makes the derivation a wire format: the hash is read as a 256-bit integer, the
animal comes from bits 8 and up, and a colour walks forward from its natural
slot until it finds one nobody has. A port that reads the digest as anything
narrower, or shifts a different number of bits, renames everybody.

**The colour walk is what stops two people looking identical.** The offset
loop is the whole point — the fallback at the end only runs when every colour
is taken, and it deliberately returns a duplicate rather than nothing.

**Edge positions are rounded to four places.** They are percentages a browser
turns into pixels, so they travel as short decimals; Python rounds half to
even, which is the trap. The corpus records values that sit exactly on a half
at the fourth place so a port using away-from-zero is caught.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_deckmux_names_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.deckmux import _names as names_module
from provide.uterm.deckmux._edge import line_to_edge_position, scroll_center_line, viewport_to_edge_range
from provide.uterm.deckmux._names import generate_color, generate_initials, generate_name

OUT = Path(__file__).with_name("deckmux_names_golden.json")

# Connection ids, chosen to land on different slots in both tables.
CONNECTION_IDS: list[str] = [
    "conn-1",
    "conn-2",
    "conn-3",
    "abc123",
    "",
    "a",
    "0" * 64,
    "user@host",
    "héllo",
    "\U0001f600",
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "The quick brown fox",
]

# How many colours after the natural slot are already taken. The cases are
# built relative to the walk's own starting point, because a fixed list of
# colours would sit somewhere unrelated to it and prove nothing.
COLOR_WALK_STEPS: list[tuple[str, int]] = [
    ("nothing taken", 0),
    ("its natural slot taken", 1),
    ("two in a row taken", 2),
    ("all but the last taken", len(names_module._COLORS) - 1),
    ("everything taken", len(names_module._COLORS)),
]

# (name, display name) — initials from a name.
INITIALS_CASES: list[tuple[str, str]] = [
    ("two words", "Red Fox"),
    ("one word", "Alice"),
    ("one short word", "A"),
    ("three words", "Mary Jane Watson"),
    ("empty", ""),
    ("leading space", " Bob Smith"),
    # Split on runs of whitespace, not on a single space: a name carrying a
    # tab or a double space still has two words in it.
    ("a tab between the words", "Bob\tSmith"),
    ("two spaces between the words", "Bob  Smith"),
    ("a newline between the words", "Bob\nSmith"),
    ("lower case", "red fox"),
    ("unicode", "Ünter Straße"),
    ("a single unicode char", "\U0001f600"),
    # Two astral characters: sliced by code point this is both, sliced by
    # UTF-16 unit it is half of the first — which is not a character at all.
    ("two astral characters", "\U0001f600\U0001f601"),
    ("an astral character and a letter", "\U0001f600x"),
]

# (name, scroll_top, visible, total) — viewport to edge bar.
EDGE_CASES: list[tuple[str, int, int, int]] = [
    ("the whole buffer visible", 0, 24, 24),
    ("the top of a long buffer", 0, 24, 100),
    ("the middle", 38, 24, 100),
    ("the bottom", 76, 24, 100),
    ("scrolled past the end", 90, 24, 100),
    ("no lines at all", 0, 24, 0),
    ("a negative total", 0, 24, -1),
    ("more visible than there are lines", 0, 100, 24),
    # Rounding: 1/3 and 2/3 do not terminate, and 0.00005 sits exactly on a
    # half at the fourth place — where Python rounds to even.
    ("a third", 1, 1, 3),
    ("two thirds", 2, 1, 3),
    ("a seventh", 1, 1, 7),
    # A thirty-second is exactly representable and lands exactly on a half at
    # the fourth place, so Python's round-half-to-even gives 0.0312 where
    # rounding away from zero gives 0.0313. This is the case that catches a
    # port reaching for the obvious multiply-round-divide.
    ("exactly on a rounding half", 1, 1, 32),
    ("the next half up", 5, 1, 32),
    ("a half that rounds the other way", 13, 1, 32),
]

# (name, line, total) — a single line to an edge position.
POSITION_CASES: list[tuple[str, int, int]] = [
    ("the first line", 0, 100),
    ("the middle", 50, 100),
    ("the last line", 100, 100),
    ("past the end", 150, 100),
    ("no lines at all", 5, 0),
    ("a negative total", 5, -1),
    ("a third", 1, 3),
    ("exactly on a rounding half", 1, 32),
    ("the next half up", 5, 32),
    ("a half that rounds the other way", 13, 32),
]

# (name, scroll_top, visible) — the centre of a viewport.
CENTER_CASES: list[tuple[str, int, int]] = [
    ("an even height", 10, 24),
    ("an odd height", 10, 25),
    ("a single line", 10, 1),
    ("no height", 10, 0),
    ("from the top", 0, 24),
]


def _record_names() -> list[dict[str, Any]]:
    """The derived name and colour for each connection id."""
    return [
        {
            "connection_id": connection_id,
            # The digest as an integer is the derivation's actual input; a port
            # that truncates it picks a different adjective.
            "hash": str(names_module._hash_int(connection_id)),
            "name": generate_name(connection_id),
            "color": generate_color(connection_id),
            "initials": generate_initials(generate_name(connection_id)),
        }
        for connection_id in CONNECTION_IDS
    ]


def _record_colors() -> list[dict[str, Any]]:
    """The colour walk, including what happens when everything is taken.

    Each case takes the *n* colours the walk would try first, so the recorded
    answer is the one after them. A fixed list of taken colours would sit
    somewhere unrelated to where this connection's walk starts and would
    record the natural pick every time, proving nothing.
    """
    colors = list(names_module._COLORS)
    start = names_module._hash_int("conn-1") % len(colors)
    records = []
    for name, steps in COLOR_WALK_STEPS:
        taken = [colors[(start + offset) % len(colors)] for offset in range(steps)]
        records.append(
            {
                "name": name,
                "steps_taken": steps,
                "taken": taken,
                "color": generate_color("conn-1", frozenset(taken)),
            }
        )
    return records


def _record_edge() -> list[dict[str, Any]]:
    """The edge bar's top and height for each viewport."""
    records = []
    for name, top, visible, total in EDGE_CASES:
        top_pct, height_pct = viewport_to_edge_range(top, visible, total)
        records.append(
            {
                "name": name,
                "scroll_top_line": top,
                "visible_lines": visible,
                "total_lines": total,
                "top_pct": top_pct,
                "height_pct": height_pct,
            }
        )
    return records


def main() -> int:
    """Write the golden corpus and report the case count."""
    corpus = {
        "adjectives": list(names_module._ADJECTIVES),
        "animals": list(names_module._ANIMALS),
        "colors": list(names_module._COLORS),
        "names": _record_names(),
        "color_walk": _record_colors(),
        "initials": [
            {"name": name, "display": display, "initials": generate_initials(display)}
            for name, display in INITIALS_CASES
        ],
        "edge": _record_edge(),
        "positions": [
            {"name": name, "line": line, "total_lines": total, "position": line_to_edge_position(line, total)}
            for name, line, total in POSITION_CASES
        ],
        "centers": [
            {"name": name, "scroll_top": top, "visible_lines": visible, "center": scroll_center_line(top, visible)}
            for name, top, visible in CENTER_CASES
        ],
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(CONNECTION_IDS)} ids, {len(EDGE_CASES)} edge cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
