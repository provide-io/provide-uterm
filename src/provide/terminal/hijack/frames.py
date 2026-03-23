#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Typed frame aliases and helper constructors for the FastAPI hijack backend."""

from __future__ import annotations

import time
from typing import Any, Literal, TypedDict, cast


class ErrorFrame(TypedDict):
    type: Literal["error"]
    message: str


class PongFrame(TypedDict):
    type: Literal["pong"]
    ts: float


class HeartbeatAckFrame(TypedDict):
    type: Literal["heartbeat_ack"]
    lease_expires_at: float
    ts: float


class WorkerConnectedFrame(TypedDict):
    type: Literal["worker_connected"]
    worker_id: str
    ts: float


class WorkerDisconnectedFrame(TypedDict):
    type: Literal["worker_disconnected"]
    worker_id: str
    ts: float


class BrowserInputFrame(TypedDict):
    type: Literal["input"]
    data: str


class TermFrame(TypedDict):
    type: Literal["term"]
    data: str
    ts: float


class SnapshotFrame(TypedDict):
    type: Literal["snapshot"]
    screen: str
    cursor: dict[str, int]
    cols: int
    rows: int
    screen_hash: str
    cursor_at_end: bool
    has_trailing_space: bool
    prompt_detected: dict[str, Any] | None
    ts: float


class AnalysisFrame(TypedDict):
    type: Literal["analysis"]
    formatted: str
    raw: Any
    ts: float


class HijackStateFrame(TypedDict):
    type: Literal["hijack_state"]
    hijacked: bool
    owner: str | None
    lease_expires_at: float | None
    input_mode: str


class WorkerStatusFrame(TypedDict, total=False):
    type: Literal["status"]
    ts: float


class HelloFrame(TypedDict, total=False):
    type: Literal["hello"]
    worker_id: str
    can_hijack: bool
    hijacked: bool
    hijacked_by_me: bool
    worker_online: bool
    input_mode: str
    role: str
    hijack_control: str
    hijack_step_supported: bool
    capabilities: dict[str, object]
    resume_supported: bool
    resume_token: str | None
    resumed: bool


def make_error_frame(message: str) -> ErrorFrame:
    return {"type": "error", "message": message}


def make_pong_frame(*, ts: float | None = None) -> PongFrame:
    return {"type": "pong", "ts": time.time() if ts is None else ts}


def make_heartbeat_ack_frame(lease_expires_at: float, *, ts: float | None = None) -> HeartbeatAckFrame:
    return {"type": "heartbeat_ack", "lease_expires_at": lease_expires_at, "ts": time.time() if ts is None else ts}


def make_worker_connected_frame(worker_id: str, *, ts: float | None = None) -> WorkerConnectedFrame:
    return {"type": "worker_connected", "worker_id": worker_id, "ts": time.time() if ts is None else ts}


def make_worker_disconnected_frame(worker_id: str, *, ts: float | None = None) -> WorkerDisconnectedFrame:
    return {"type": "worker_disconnected", "worker_id": worker_id, "ts": time.time() if ts is None else ts}


def make_term_frame(data: str, *, ts: float | None = None) -> TermFrame:
    return {"type": "term", "data": data, "ts": time.time() if ts is None else ts}


def make_snapshot_frame(
    *,
    screen: str,
    cursor: dict[str, int],
    cols: int,
    rows: int,
    screen_hash: str,
    cursor_at_end: bool,
    has_trailing_space: bool,
    prompt_detected: dict[str, Any] | None,
    ts: float,
) -> SnapshotFrame:
    return {
        "type": "snapshot",
        "screen": screen,
        "cursor": cursor,
        "cols": cols,
        "rows": rows,
        "screen_hash": screen_hash,
        "cursor_at_end": cursor_at_end,
        "has_trailing_space": has_trailing_space,
        "prompt_detected": prompt_detected,
        "ts": ts,
    }


def make_analysis_frame(*, formatted: str, raw: Any, ts: float | None = None) -> AnalysisFrame:
    return {"type": "analysis", "formatted": formatted, "raw": raw, "ts": time.time() if ts is None else ts}


def make_hijack_state_frame(
    *,
    hijacked: bool,
    owner: str | None,
    lease_expires_at: float | None,
    input_mode: str,
) -> HijackStateFrame:
    return {
        "type": "hijack_state",
        "hijacked": hijacked,
        "owner": owner,
        "lease_expires_at": lease_expires_at,
        "input_mode": input_mode,
    }


def make_hello_frame(**payload: Any) -> HelloFrame:
    return cast("HelloFrame", {"type": "hello", **payload})


def coerce_worker_status_frame(payload: dict[str, Any]) -> WorkerStatusFrame:
    frame = dict(payload)
    frame.setdefault("type", "status")
    frame.setdefault("ts", time.time())
    return cast("WorkerStatusFrame", frame)
