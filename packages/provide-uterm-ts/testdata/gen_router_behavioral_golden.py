#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript router-behavioral port.

These heuristics feed a policy gate that can close a connection, so the exact
numbers matter: characters per second and the jitter of the inter-keystroke
intervals are what an operator's threshold is compared against.

``jitter`` is ``statistics.variance``, which is the *sample* variance with an
``n - 1`` denominator. A port using the population formula would report a
systematically smaller number and quietly shift every configured threshold.

Two shapes return zeros rather than a computed value: fewer than two
keystrokes gives nothing to measure, and a run of keystrokes sharing one
timestamp gives a zero duration that would otherwise divide by zero. Both are
reachable — the second is what a paste looks like.

The ring holds fifty timestamps, so a long typing session measures a rolling
window rather than the whole session.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_router_behavioral_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

from provide.uterm.server.bridge.hub.router_behavioral import (
    forget_browser,
    get_heuristics,
    record_keystroke,
)

if TYPE_CHECKING:
    from collections import deque

OUT = Path(__file__).with_name("router_behavioral_golden.json")

KEYSTROKE_RING_MAX = 50

# (name, timestamps fed in order)
HEURISTIC_CASES: list[tuple[str, list[float]]] = [
    ("no keystrokes", []),
    ("one keystroke", [1000.0]),
    ("two evenly spaced", [1000.0, 1000.5]),
    ("three evenly spaced", [1000.0, 1000.5, 1001.0]),
    ("fast and even", [1000.0, 1000.1, 1000.2, 1000.3]),
    ("human-ish, uneven", [1000.0, 1000.12, 1000.31, 1000.38, 1000.72]),
    ("machine-even, no jitter", [1000.0, 1000.05, 1000.10, 1000.15, 1000.20]),
    ("one long pause", [1000.0, 1000.05, 1000.10, 1005.0]),
    ("all at the same instant", [1000.0, 1000.0, 1000.0]),
    ("two at the same instant", [1000.0, 1000.0]),
    ("sub-millisecond", [1000.0, 1000.0001, 1000.0002]),
]


class _Router:
    """The slice of MessageRouter the behavioral helpers touch."""

    def __init__(self) -> None:
        self.keystroke_timestamps: dict[Any, deque[float]] = {}


def _feed(timestamps: list[float]) -> dict[str, float]:
    """Record every timestamp for one browser and read the heuristics back."""
    router = _Router()
    clock = {"now": 0.0}
    with mock.patch("time.monotonic", side_effect=lambda: clock["now"]):
        for value in timestamps:
            clock["now"] = value
            record_keystroke(router, "ws")  # type: ignore[arg-type]
    return get_heuristics(router, "ws")  # type: ignore[arg-type]


def _ring_record() -> dict[str, Any]:
    """The ring keeps the newest fifty timestamps and drops the rest."""
    router = _Router()
    clock = {"now": 0.0}
    with mock.patch("time.monotonic", side_effect=lambda: clock["now"]):
        for index in range(KEYSTROKE_RING_MAX + 10):
            clock["now"] = 1000.0 + index
            record_keystroke(router, "ws")  # type: ignore[arg-type]
        overflowed = get_heuristics(router, "ws")  # type: ignore[arg-type]
    ring = router.keystroke_timestamps["ws"]
    return {
        "maxlen": ring.maxlen,
        "length": len(ring),
        "first": ring[0],
        "last": ring[-1],
        "cps": overflowed["cps"],
        "jitter": overflowed["jitter"],
    }


def _isolation_record() -> dict[str, Any]:
    """Browsers are tracked separately, and forgetting one leaves the other."""
    router = _Router()
    clock = {"now": 1000.0}
    with mock.patch("time.monotonic", side_effect=lambda: clock["now"]):
        record_keystroke(router, "a")  # type: ignore[arg-type]
        clock["now"] = 1000.5
        record_keystroke(router, "a")  # type: ignore[arg-type]
        record_keystroke(router, "b")  # type: ignore[arg-type]
        before_a = get_heuristics(router, "a")  # type: ignore[arg-type]
        before_b = get_heuristics(router, "b")  # type: ignore[arg-type]
        forget_browser(router, "a")  # type: ignore[arg-type]
        # Forgetting an unknown browser is a no-op rather than an error.
        forget_browser(router, "never-seen")  # type: ignore[arg-type]
        after_a = get_heuristics(router, "a")  # type: ignore[arg-type]
        after_b = get_heuristics(router, "b")  # type: ignore[arg-type]
    return {
        "before_a": before_a,
        "before_b": before_b,
        "after_a": after_a,
        "after_b": after_b,
        "tracked_after_forget": sorted(router.keystroke_timestamps),
    }


def main() -> int:
    """Write the golden corpus and report the case count."""
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_router_behavioral_golden.py",
        "ring_max": KEYSTROKE_RING_MAX,
        "heuristics": [
            {"name": name, "timestamps": timestamps, **_feed(timestamps)} for name, timestamps in HEURISTIC_CASES
        ],
        "ring": _ring_record(),
        "isolation": _isolation_record(),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(HEURISTIC_CASES)} heuristic cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
