#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-server
"""Generate the differential golden corpus for the hub's frame builders.

Every frame the hub sends a browser, built once so no route hand-rolls one.

**Whether a null survives is per-frame and deliberate.** Most builders drop an
absent field, because a browser reading `undefined` and one reading a missing
key behave the same. Two do not: a hijack state's `owner` and
`lease_expires_at` are read directly off the frame, so a session with no owner
has to say so rather than omit it, and an analysis frame's `raw` is rendered by
the frontend whether or not there is any.

**A timestamp is stamped when the frame is built, not when it is sent**, and a
caller may supply one — which is the only reason these are testable at all.

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_hubframes_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.server.bridge.frames import (
    coerce_worker_status_frame,
    make_analysis_frame,
    make_error_frame,
    make_heartbeat_ack_frame,
    make_hello_frame,
    make_hijack_state_frame,
    make_pong_frame,
    make_term_frame,
    make_worker_connected_frame,
    make_worker_disconnected_frame,
)

OUT = Path(__file__).with_name("hubframes_golden.json")

TS = 1_700_000_000.5


def _build() -> dict[str, Any]:
    """One of every frame, with the nulls that matter."""
    return {
        "ts": TS,
        "frames": [
            {"name": "an error", "frame": make_error_frame("something went wrong")},
            {"name": "an error with no message", "frame": make_error_frame("")},
            {"name": "a pong", "frame": make_pong_frame(ts=TS)},
            {"name": "a heartbeat ack", "frame": make_heartbeat_ack_frame(TS + 30, ts=TS)},
            {"name": "a worker connected", "frame": make_worker_connected_frame("w1", ts=TS)},
            {"name": "a worker disconnected", "frame": make_worker_disconnected_frame("w1", ts=TS)},
            {"name": "terminal output", "frame": make_term_frame("hello", ts=TS)},
            {"name": "terminal output that is empty", "frame": make_term_frame("", ts=TS)},
            # An analysis keeps a null raw: the frontend reads it directly.
            {"name": "an analysis", "frame": make_analysis_frame(formatted="done", raw={"x": 1}, ts=TS)},
            {"name": "an analysis with no raw", "frame": make_analysis_frame(formatted="done", raw=None, ts=TS)},
            # A hijack state keeps both nulls, for the same reason.
            {
                "name": "a hijack in progress",
                "frame": make_hijack_state_frame(
                    hijacked=True, owner="u1", lease_expires_at=TS + 60, input_mode="hijack"
                ),
            },
            {
                "name": "no hijack",
                "frame": make_hijack_state_frame(hijacked=False, owner=None, lease_expires_at=None, input_mode="open"),
            },
            # A hello carries whatever capabilities the caller adds.
            {"name": "a hello", "frame": make_hello_frame(worker_id="w1")},
            {"name": "a hello with capabilities", "frame": make_hello_frame(worker_id="w1", replay_supported=True)},
            {"name": "a hello overriding a default", "frame": make_hello_frame(mcp_supported=False)},
            {"name": "a hello overriding its type", "frame": make_hello_frame(type="not_hello")},
            # A status frame is whatever the worker sent, with the gaps filled.
            {"name": "a worker status", "frame": coerce_worker_status_frame({"state": "running", "ts": TS})},
            {"name": "a status with no type", "frame": coerce_worker_status_frame({"state": "running", "ts": TS})},
            {"name": "a status naming its own type", "frame": coerce_worker_status_frame({"type": "custom", "ts": TS})},
            {"name": "an empty status", "frame": _stamped(coerce_worker_status_frame({}))},
        ],
    }


def _stamped(frame: dict[str, Any]) -> dict[str, Any]:
    """A frame whose timestamp was supplied by the clock, replaced so it is stable."""
    return {**frame, "ts": TS}


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    # Not sorted: a frame's field order is what goes on the wire.
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(corpus['frames'])} frames)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
