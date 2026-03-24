#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from provide.terminal.hijack.frames import (
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


def test_make_worker_connection_frames() -> None:
    connected = make_worker_connected_frame("bot1", ts=1.0)
    disconnected = make_worker_disconnected_frame("bot1", ts=2.0)
    assert connected == {"type": "worker_connected", "worker_id": "bot1", "ts": 1.0}
    assert disconnected == {"type": "worker_disconnected", "worker_id": "bot1", "ts": 2.0}


def test_make_snapshot_frame() -> None:
    snapshot = make_snapshot_frame(
        screen="hello",
        cursor={"x": 1, "y": 2},
        cols=80,
        rows=25,
        screen_hash="abc",
        cursor_at_end=True,
        has_trailing_space=False,
        prompt_detected={"prompt_id": "menu"},
        ts=3.0,
    )
    assert snapshot["type"] == "snapshot"
    assert snapshot["prompt_detected"] == {"prompt_id": "menu"}


def test_make_misc_frames() -> None:
    assert make_error_frame("bad") == {"type": "error", "message": "bad"}
    assert make_pong_frame(ts=4.0) == {"type": "pong", "ts": 4.0}
    assert make_heartbeat_ack_frame(10.0, ts=5.0) == {"type": "heartbeat_ack", "lease_expires_at": 10.0, "ts": 5.0}
    assert make_term_frame("abc", ts=5.5) == {"type": "term", "data": "abc", "ts": 5.5}
    assert make_analysis_frame(formatted="ok", raw=None, ts=6.0) == {
        "type": "analysis",
        "formatted": "ok",
        "raw": None,
        "ts": 6.0,
    }


def test_make_hello_and_hijack_state_frames() -> None:
    hello = make_hello_frame(worker_id="bot1", role="admin", worker_online=True)
    hijack_state = make_hijack_state_frame(hijacked=True, owner="me", lease_expires_at=7.0, input_mode="hijack")
    assert hello["type"] == "hello"
    assert hello["role"] == "admin"
    assert hijack_state == {
        "type": "hijack_state",
        "hijacked": True,
        "owner": "me",
        "lease_expires_at": 7.0,
        "input_mode": "hijack",
    }


def test_coerce_worker_status_frame_adds_defaults() -> None:
    status = coerce_worker_status_frame({"worker_online": True})
    assert status["type"] == "status"
    assert "ts" in status
