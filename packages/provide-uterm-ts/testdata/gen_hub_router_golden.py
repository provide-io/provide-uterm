#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript hub-router port.

The router is the hub's outbound plumbing, and three of its decisions are
recorded here because getting any of them wrong is silent rather than loud.

**Frame encoding.** A ``term`` message goes to a browser as raw terminal
data; everything else is a framed control envelope. The dispatch mirrors the
worker-bound one and has the same consequence in reverse — a control frame
sent down the terminal path would be rendered as text on the screen.

**The owner label.** Every browser is told whether a session is held, and by
whom, as one of three values: ``me`` for the holder, ``other`` for anyone
else, and null for nobody. The label is per-recipient, so the same broadcast
produces different frames for different browsers, and a REST lease is always
``other`` because no browser holds it.

**Event redaction and truncation, in that order.** Content events are
redacted with the server-default ruleset *before* the terminal payload is
capped, so a secret sitting near the truncation boundary is removed whichever
side of the cut it lands on. Doing it the other way round would leak exactly
the strings the live broadcast scrubs.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_hub_router_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.server.bridge.frames import make_hijack_state_frame
from provide.uterm.server.bridge.hub.core_helpers import _encode_browser_frame

OUT = Path(__file__).with_name("hub_router_golden.json")

# (name, message)
BROWSER_FRAME_CASES: list[tuple[str, dict[str, Any]]] = [
    ("term data", {"type": "term", "data": "hello\r\n"}),
    ("term with no data", {"type": "term"}),
    ("term with empty data", {"type": "term", "data": ""}),
    ("term carrying escapes", {"type": "term", "data": "\x1b[31mred\x1b[0m"}),
    ("snapshot", {"type": "snapshot", "screen": "hello", "ts": 1.5}),
    ("analysis", {"type": "analysis", "summary": "idle"}),
    ("hijack state", {"type": "hijack_state", "hijacked": True, "owner": "me"}),
    ("no type", {"data": "hello"}),
    ("null type", {"type": None, "data": "hello"}),
    ("empty type", {"type": "", "data": "hello"}),
    ("non-string type", {"type": 3, "data": "hello"}),
]

# (name, is_dashboard, is_rest, recipient holds the lease)
OWNER_CASES: list[tuple[str, bool, bool, bool]] = [
    ("nobody holds it", False, False, False),
    ("dashboard, held by me", True, False, True),
    ("dashboard, held by someone else", True, False, False),
    ("rest lease", False, True, False),
    ("rest lease, and I hold a stale slot", False, True, True),
    ("both, held by me", True, True, True),
    ("both, held by someone else", True, True, False),
]


def _owner_label(is_dashboard: bool, is_rest: bool, holds_lease: bool) -> str | None:
    """Reproduce the router's per-recipient owner label."""
    if is_dashboard and holds_lease:
        return "me"
    if is_dashboard or is_rest:
        return "other"
    return None


def main() -> int:
    """Write the golden corpus and report the case count."""
    frames = [
        {"name": name, "message": message, "encoded": _encode_browser_frame(message)}
        for name, message in BROWSER_FRAME_CASES
    ]

    owners = []
    for name, is_dashboard, is_rest, holds_lease in OWNER_CASES:
        owner = _owner_label(is_dashboard, is_rest, holds_lease)
        owners.append(
            {
                "name": name,
                "is_dashboard": is_dashboard,
                "is_rest": is_rest,
                "holds_lease": holds_lease,
                "owner": owner,
                "frame": make_hijack_state_frame(
                    hijacked=is_dashboard or is_rest,
                    owner=owner,
                    lease_expires_at=None,
                    input_mode="hijack",
                ),
            }
        )

    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_hub_router_golden.py",
        "browser_frames": frames,
        "owners": owners,
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(frames)} frame cases, {len(owners)} owner cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
