#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript contracts port.

The handshake gate. Peers advertise a supported version range, the server
intersects it with its own and takes the highest version both sides can
speak — or refuses, and the socket closes 1002.

Both outcomes are load-bearing. Picking too low silently downgrades a pair
that could have spoken something newer; failing to refuse at all lets two
peers proceed while disagreeing about the wire format, which surfaces later
as corrupt frames rather than as a clean disconnect.

The matrix is enumerated across and beyond the server's own range so the
boundaries are pinned from both sides, including the reversed range a
confused client can send.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_contracts_golden.py
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any, get_args

from provide.uterm.bridge.contracts import (
    CURRENT_PROTOCOL_VERSION,
    MAX_PROTOCOL_VERSION,
    MIN_PROTOCOL_VERSION,
    PREFERRED_PROTOCOL_VERSION,
    FrameType,
    InputMode,
    SessionLifecycle,
    Visibility,
    negotiate_protocol_version,
)

OUT = Path(__file__).with_name("contracts_golden.json")


def main() -> int:
    """Write the golden corpus and report the case count."""
    negotiations: list[dict[str, Any]] = []
    for client_min, client_max in itertools.product(range(-1, 4), repeat=2):
        negotiations.append(
            {
                "client_min": client_min,
                "client_max": client_max,
                "selected": negotiate_protocol_version(client_min, client_max),
            }
        )

    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_contracts_golden.py",
        "min_protocol_version": MIN_PROTOCOL_VERSION,
        "max_protocol_version": MAX_PROTOCOL_VERSION,
        "preferred_protocol_version": PREFERRED_PROTOCOL_VERSION,
        "current_protocol_version": CURRENT_PROTOCOL_VERSION,
        "negotiations": negotiations,
        "session_lifecycles": list(get_args(SessionLifecycle)),
        "input_modes": list(get_args(InputMode)),
        "visibilities": list(get_args(Visibility)),
        "frame_types": list(get_args(FrameType)),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(negotiations)} negotiation cases, {len(payload['frame_types'])} frame types)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
