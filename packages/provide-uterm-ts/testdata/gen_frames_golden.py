#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript ``frames`` port.

The frame types themselves are generated from the Pydantic source by
``scripts/codegen_frames.py``. This corpus covers the *builders*, whose
subtlety is which fields survive a null value: some builders drop them and
some keep them on the wire, and the frontend reads several of the kept ones
directly.

Every timestamp is passed in explicitly so the corpus is reproducible.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_frames_golden.py
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
    make_snapshot_frame,
    make_term_frame,
    make_worker_connected_frame,
    make_worker_disconnected_frame,
)

OUT = Path(__file__).with_name("frames_golden.json")

# A fixed timestamp so the corpus does not move between runs.
TS = 1735689600.5


def _snapshot_cases() -> list[dict[str, Any]]:
    """Snapshot frames, including the null fields that must survive."""
    base: dict[str, Any] = {
        "screen": "hello",
        "cursor": {"x": 1, "y": 2},
        "cols": 80,
        "rows": 24,
        "screen_hash": "deadbeef",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": None,
        "ts": TS,
    }
    cases = [
        ("null prompt is kept on the wire", base),
        ("absent raw_tail", {**base, "raw_tail": None}),
        ("raw_tail present", {**base, "raw_tail": "tail"}),
        ("prompt detected", {**base, "prompt_detected": {"kind": "command", "confidence": 0.75}}),
        ("empty screen", {**base, "screen": ""}),
        ("cursor at origin", {**base, "cursor": {"x": 0, "y": 0}}),
    ]
    return [{"name": name, "kwargs": kwargs, "frame": make_snapshot_frame(**kwargs)} for name, kwargs in cases]


def main() -> int:
    """Write the golden corpus and report the record count."""
    payload: dict[str, Any] = {
        "generator": "packages/provide-uterm-ts/testdata/gen_frames_golden.py",
        "ts": TS,
        "error": [
            {"message": "boom", "frame": make_error_frame("boom")},
            {"message": "", "frame": make_error_frame("")},
        ],
        "pong": [{"ts": TS, "frame": make_pong_frame(ts=TS)}],
        "heartbeat_ack": [
            {"lease_expires_at": TS + 30, "ts": TS, "frame": make_heartbeat_ack_frame(TS + 30, ts=TS)},
            {"lease_expires_at": 0.0, "ts": TS, "frame": make_heartbeat_ack_frame(0.0, ts=TS)},
        ],
        "worker_connected": [{"worker_id": "w1", "ts": TS, "frame": make_worker_connected_frame("w1", ts=TS)}],
        "worker_disconnected": [{"worker_id": "w1", "ts": TS, "frame": make_worker_disconnected_frame("w1", ts=TS)}],
        "term": [
            {"data": "hello", "ts": TS, "frame": make_term_frame("hello", ts=TS)},
            {"data": "", "ts": TS, "frame": make_term_frame("", ts=TS)},
            # High bytes arrive as latin-1 shim characters and must survive.
            {"data": "\xff\xfe", "ts": TS, "frame": make_term_frame("\xff\xfe", ts=TS)},
        ],
        "snapshot": _snapshot_cases(),
        "analysis": [
            # raw=None must serialise as null, not be dropped.
            {"formatted": "f", "raw": None, "ts": TS, "frame": make_analysis_frame(formatted="f", raw=None, ts=TS)},
            {
                "formatted": "f",
                "raw": {"a": 1},
                "ts": TS,
                "frame": make_analysis_frame(formatted="f", raw={"a": 1}, ts=TS),
            },
            {"formatted": "", "raw": [], "ts": TS, "frame": make_analysis_frame(formatted="", raw=[], ts=TS)},
        ],
        "hijack_state": [
            # owner / lease_expires_at can legitimately be null and are kept.
            {
                "hijacked": False,
                "owner": None,
                "lease_expires_at": None,
                "input_mode": "read_only",
                "frame": make_hijack_state_frame(
                    hijacked=False, owner=None, lease_expires_at=None, input_mode="read_only"
                ),
            },
            {
                "hijacked": True,
                "owner": "user:alice",
                "lease_expires_at": TS + 30,
                "input_mode": "read_write",
                "frame": make_hijack_state_frame(
                    hijacked=True, owner="user:alice", lease_expires_at=TS + 30, input_mode="read_write"
                ),
            },
        ],
        "hello": [
            {"payload": {}, "frame": make_hello_frame()},
            {"payload": {"mcp_supported": False}, "frame": make_hello_frame(mcp_supported=False)},
            {"payload": {"vnc_supported": False}, "frame": make_hello_frame(vnc_supported=False)},
            # Arbitrary capability flags pass through unmodelled.
            {"payload": {"resume_supported": True}, "frame": make_hello_frame(resume_supported=True)},
            {"payload": {"protocol": {"min": 1, "max": 3}}, "frame": make_hello_frame(protocol={"min": 1, "max": 3})},
        ],
        "worker_status": [
            {"payload": {}, "frame": coerce_worker_status_frame({"ts": TS})},
            {"payload": {"type": "custom"}, "frame": coerce_worker_status_frame({"type": "custom", "ts": TS})},
            {"payload": {"state": "idle"}, "frame": coerce_worker_status_frame({"state": "idle", "ts": TS})},
        ],
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    total = sum(len(v) for v in payload.values() if isinstance(v, list))
    print(f"wrote {OUT} ({total} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
