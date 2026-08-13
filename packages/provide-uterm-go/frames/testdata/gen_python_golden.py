#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the bridge-frame golden from the REAL Python builders
(provide.uterm.server.bridge.frames + control_channel_builders). Run from the
repo root:

    uv run python packages/provide-uterm-go/frames/testdata/gen_python_golden.py

Writes python_golden.json next to this script. golden_test.go asserts Go's
frame table marshals to the same wire JSON for every case here.

Every `ts` is an explicit constant. The builders default it to time.time(), so
a generator that let them do that would rewrite this file on every run and the
drift check would be permanently red — the fixed values are what make the
corpus re-derivable at all.

Regenerating this in 2026-08 surfaced why the corpus needed a generator: it was
recorded on 2026-07-09, `5145daae` added MCP/VNC capability negotiation on
07-19, and nothing re-derived it in between. The hello frame had been missing
mcp_supported/vnc_supported ever since, with no check able to notice.
"""

from __future__ import annotations

import json
import pathlib

from provide.uterm.control_channel_builders import (
    make_identity,
    make_link_patterns,
    make_presence_update,
    make_resume,
    make_resume_failed,
    make_resume_ok,
    make_session_token,
)
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


def build() -> dict[str, object]:
    return {
        "analysis_null_raw": make_analysis_frame(formatted="f", raw=None, ts=4.5),
        "analysis_raw": make_analysis_frame(formatted="f", raw={"k": [1, 2]}, ts=5.5),
        "error": make_error_frame("boom"),
        "heartbeat_ack": make_heartbeat_ack_frame(456.25, ts=123.5),
        "hello": make_hello_frame(
            worker_id="w1",
            can_hijack=True,
            hijacked=False,
            worker_online=True,
            input_mode="raw",
            protocol={"selected": 2, "server_min": 1, "server_max": 2},
        ),
        "hijack_state_off": make_hijack_state_frame(
            hijacked=False, owner=None, lease_expires_at=None, input_mode="raw"
        ),
        "hijack_state_on": make_hijack_state_frame(
            hijacked=True, owner="alice", lease_expires_at=99.5, input_mode="cooked"
        ),
        "identity_defaults": make_identity("user:bob"),
        "identity_full": make_identity(
            "user:alice",
            claims={"role": "admin", "n": 3},
            fingerprint="SHA256:fp",
            transport="ws",
        ),
        "link_patterns": make_link_patterns(
            [
                {
                    "action": "cmd",
                    "group": 1,
                    "id": "p1",
                    "pattern": r"foo(\d+)",
                    "payload": "run {1}",
                },
                {
                    "action": "url",
                    "class": "link",
                    "flags": "i",
                    "hover": "open",
                    "line_contains": "http",
                    "pattern": r"https?://\S+",
                },
            ]
        ),
        "pong": make_pong_frame(ts=123.5),
        "presence_update": make_presence_update("u1", scroll_line=5, typing=True),
        "resume": make_resume("rtok", player_id=7),
        "resume_failed": make_resume_failed("expired"),
        "resume_ok": make_resume_ok(),
        "session_token": make_session_token("tok", player_id=3),
        "session_token_no_player": make_session_token("tok2"),
        "snapshot_full": make_snapshot_frame(
            screen="s",
            cursor={"x": 0, "y": 0},
            cols=132,
            rows=43,
            screen_hash="h",
            cursor_at_end=False,
            has_trailing_space=True,
            prompt_detected={"confidence": 0.75, "prompt_id": "shell"},
            ts=10.5,
            raw_tail="tail\x1b[1m",
        ),
        "snapshot_minimal": make_snapshot_frame(
            screen="line1\nline2",
            cursor={"x": 1, "y": 2},
            cols=80,
            rows=25,
            screen_hash="abc123",
            cursor_at_end=True,
            has_trailing_space=False,
            prompt_detected=None,
            ts=9.5,
        ),
        "status": coerce_worker_status_frame({"cpu": 12.5, "tag": "ok", "ts": 6.5}),
        "term": make_term_frame("hi\x1b[0mé", ts=3.5),
        "worker_connected": make_worker_connected_frame("w1", ts=1.5),
        "worker_disconnected": make_worker_disconnected_frame("w1", ts=2.5),
    }


def main() -> None:
    corpus = build()
    out = pathlib.Path(__file__).with_name("python_golden.json")
    # indent=2 and NO trailing newline — this corpus's own recorded style.
    out.write_text(json.dumps(corpus, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {out} ({len(corpus)} cases)")


if __name__ == "__main__":
    main()
