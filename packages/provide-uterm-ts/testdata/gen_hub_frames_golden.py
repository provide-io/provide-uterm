#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript hub-frames port.

``_encode_worker_frame`` picks the wire encoding from the message type, and
getting that wrong is not a formatting bug: an ``input`` message must go out
as raw terminal data, while everything else is DLE/STX-framed control JSON.
Sending a control frame down the terminal path would feed JSON to the PTY.

The dispatch is on ``str(msg.get("type") or "")``, which folds a missing key,
``None`` and an empty string together, and coerces a non-string type before
comparing. The table drives each of those.

``_mono_to_wall`` converts a monotonic timestamp for external consumers, and
passes ``None`` through rather than converting it.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_hub_frames_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

from provide.uterm.server.bridge.hub.core_helpers import _encode_worker_frame, _mono_to_wall

OUT = Path(__file__).with_name("hub_frames_golden.json")

WALL = 1_700_000_000.0
MONO = 1000.0

# (name, message)
FRAME_CASES: list[tuple[str, dict[str, Any]]] = [
    ("input goes out as terminal data", {"type": "input", "data": "ls\r"}),
    ("input with no data", {"type": "input"}),
    ("input with empty data", {"type": "input", "data": ""}),
    ("input carrying control bytes", {"type": "input", "data": "\x1b[A"}),
    ("control frame", {"type": "control", "action": "pause", "owner": "cli"}),
    ("resume frame", {"type": "control", "action": "resume", "owner": "lease-expired", "lease_s": 0}),
    ("snapshot request", {"type": "snapshot_req", "req_id": "r1"}),
    ("no type at all", {"action": "pause"}),
    ("null type", {"type": None, "action": "pause"}),
    ("empty type", {"type": "", "action": "pause"}),
    ("non-string type", {"type": 7, "action": "pause"}),
]


def main() -> int:
    """Write the golden corpus and report the case count."""
    frames = [
        {"name": name, "message": message, "encoded": _encode_worker_frame(message)} for name, message in FRAME_CASES
    ]

    with mock.patch("time.time", return_value=WALL), mock.patch("time.monotonic", return_value=MONO):
        mono_to_wall = [
            {"mono": None, "wall": _mono_to_wall(None)},
            {"mono": MONO, "wall": _mono_to_wall(MONO)},
            {"mono": MONO + 30.0, "wall": _mono_to_wall(MONO + 30.0)},
            {"mono": MONO - 30.0, "wall": _mono_to_wall(MONO - 30.0)},
            {"mono": 0.0, "wall": _mono_to_wall(0.0)},
        ]

    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_hub_frames_golden.py",
        "wall": WALL,
        "mono": MONO,
        "frames": frames,
        "mono_to_wall": mono_to_wall,
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(frames)} frame cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
