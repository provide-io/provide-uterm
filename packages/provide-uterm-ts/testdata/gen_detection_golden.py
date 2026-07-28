#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript ``detection`` port.

Covers the input-type heuristic and the screen buffer's timing metadata.
Timestamps are supplied explicitly so the corpus is reproducible.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_detection_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.detection.buffer import BufferManager
from provide.uterm.detection.input_type import auto_detect_input_type

OUT = Path(__file__).with_name("detection_golden.json")

# Screens for the input-type heuristic. The phrase lists are checked in order,
# so a screen carrying phrases from two tiers resolves to the earlier one.
INPUT_TYPE_SCREENS: list[str] = [
    "",
    "nothing in particular",
    # any_key tier.
    "Press any key to continue",
    "press a key",
    "Hit any key",
    "STRIKE ANY KEY",
    "<MORE>",
    "[more]",
    "-- More --",
    # single_key tier.
    "Continue? (y/n)",
    "Are you sure (yes/no)",
    "continue?",
    "quit?",
    "abort?",
    "retry?",
    "Proceed [Y/N]",
    "(Q)uit or (A)bort",
    # multi_key tier.
    "Enter your name",
    "Type a command",
    "input:",
    "Name:",
    "Password:",
    "Username:",
    "Choose:",
    "Select:",
    "Command:",
    "Search:",
    # Tier precedence: an earlier tier wins even when a later phrase is present.
    "Press any key, then enter your name",
    "Continue? (y/n) — or type a command",
    # Matching is case-insensitive and substring-based.
    "PRESS ANY KEY",
    "xxpress any keyxx",
    # A screen with none of the phrases falls through to multi_key.
    "1234567890",
]

# (name, snapshots) — each snapshot is fed to one BufferManager in order.
BUFFER_CASES: list[tuple[str, list[dict[str, Any]]]] = [
    ("single screen", [{"screen": "a", "screen_hash": "h1", "captured_at": 100.0}]),
    (
        "unchanged screen accumulates idle time",
        [
            {"screen": "a", "screen_hash": "h1", "captured_at": 100.0},
            {"screen": "a", "screen_hash": "h1", "captured_at": 102.5},
            {"screen": "a", "screen_hash": "h1", "captured_at": 105.0},
        ],
    ),
    (
        "changed screen restarts the clock",
        [
            {"screen": "a", "screen_hash": "h1", "captured_at": 100.0},
            {"screen": "b", "screen_hash": "h2", "captured_at": 103.0},
            {"screen": "b", "screen_hash": "h2", "captured_at": 104.0},
        ],
    ),
    (
        "alternating screens",
        [
            {"screen": "a", "screen_hash": "h1", "captured_at": 10.0},
            {"screen": "b", "screen_hash": "h2", "captured_at": 11.0},
            {"screen": "a", "screen_hash": "h1", "captured_at": 12.0},
        ],
    ),
]


def _buffer_record(name: str, snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    """Drive one BufferManager and record every buffer it produced."""
    manager = BufferManager(max_size=3)
    produced = []
    for snapshot in snapshots:
        buffer = manager.add_screen(snapshot)
        produced.append(
            {
                "screen": buffer.screen,
                "screen_hash": buffer.screen_hash,
                "captured_at": buffer.captured_at,
                "time_since_last_change": buffer.time_since_last_change,
                "matched_prompt_id": buffer.matched_prompt_id,
            }
        )
    return {
        "name": name,
        "snapshots": snapshots,
        "buffers": produced,
        "recent_2": [b.screen_hash for b in manager.get_recent(2)],
        "recent_all": [b.screen_hash for b in manager.get_recent(99)],
    }


def _eviction_record() -> dict[str, Any]:
    """A buffer bounded at three entries drops the oldest."""
    manager = BufferManager(max_size=3)
    for i in range(5):
        manager.add_screen({"screen": str(i), "screen_hash": f"h{i}", "captured_at": float(i)})
    return {"kept": [b.screen_hash for b in manager.get_recent(99)]}


def main() -> int:
    """Write the golden corpus and report the record count."""
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_detection_golden.py",
        "input_type": [{"screen": s, "type": auto_detect_input_type(s)} for s in INPUT_TYPE_SCREENS],
        "buffers": [_buffer_record(name, snaps) for (name, snaps) in BUFFER_CASES],
        "eviction": _eviction_record(),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    total = len(payload["input_type"]) + len(payload["buffers"])
    print(f"wrote {OUT} ({total} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
