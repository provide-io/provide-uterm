#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Typed-dict aliases and builder helpers for hijack-bridge wire frames.

The Pydantic ``schemas.py`` module in ``provide.uterm.bridge`` is the
single source of truth for frame shapes; builders here construct those
models and dump them so wire bytes stay byte-for-byte stable.

The local ``TypedDict`` aliases survive as the *return type* of each
builder so existing callers and downstream signatures don't have to
churn — they still see ``ErrorFrame``, ``TermFrame``, etc.
"""

from __future__ import annotations

import time
from typing import Any, Literal, TypedDict, cast

from provide.uterm.bridge.schemas import (
    AnalysisFrame as _AnalysisModel,
)
from provide.uterm.bridge.schemas import (
    ErrorFrame as _ErrorModel,
)
from provide.uterm.bridge.schemas import (
    HeartbeatAckFrame as _HeartbeatAckModel,
)
from provide.uterm.bridge.schemas import (
    HijackStateFrame as _HijackStateModel,
)
from provide.uterm.bridge.schemas import (
    PongFrame as _PongModel,
)
from provide.uterm.bridge.schemas import (
    TermFrame as _TermModel,
)
from provide.uterm.bridge.schemas import (
    WorkerConnectedFrame as _WorkerConnectedModel,
)
from provide.uterm.bridge.schemas import (
    WorkerDisconnectedFrame as _WorkerDisconnectedModel,
)
from provide.uterm.frames import make_snapshot_frame as _core_make_snapshot_frame


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
    raw_tail: str | None
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
    protocol_version: int
    # Range-negotiation handshake. Server hello sets
    # ``protocol={"selected": N, "server_min": MIN, "server_max": MAX}``.
    # Worker hello sets ``protocol={"min": ..., "max": ..., "preferred": ...}``.
    protocol: dict[str, int]


def make_error_frame(message: str) -> ErrorFrame:
    return cast("ErrorFrame", _ErrorModel(type="error", message=message).model_dump(exclude_none=True))


def make_pong_frame(*, ts: float | None = None) -> PongFrame:
    return cast(
        "PongFrame",
        _PongModel(type="pong", ts=time.time() if ts is None else ts).model_dump(exclude_none=True),
    )


def make_heartbeat_ack_frame(lease_expires_at: float, *, ts: float | None = None) -> HeartbeatAckFrame:
    return cast(
        "HeartbeatAckFrame",
        _HeartbeatAckModel(
            type="heartbeat_ack",
            lease_expires_at=lease_expires_at,
            ts=time.time() if ts is None else ts,
        ).model_dump(exclude_none=True),
    )


def make_worker_connected_frame(worker_id: str, *, ts: float | None = None) -> WorkerConnectedFrame:
    return cast(
        "WorkerConnectedFrame",
        _WorkerConnectedModel(
            type="worker_connected",
            worker_id=worker_id,
            ts=time.time() if ts is None else ts,
        ).model_dump(exclude_none=True),
    )


def make_worker_disconnected_frame(worker_id: str, *, ts: float | None = None) -> WorkerDisconnectedFrame:
    return cast(
        "WorkerDisconnectedFrame",
        _WorkerDisconnectedModel(
            type="worker_disconnected",
            worker_id=worker_id,
            ts=time.time() if ts is None else ts,
        ).model_dump(exclude_none=True),
    )


def make_term_frame(data: str, *, ts: float | None = None) -> TermFrame:
    return cast(
        "TermFrame",
        _TermModel(type="term", data=data, ts=time.time() if ts is None else ts).model_dump(exclude_none=True),
    )


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
    raw_tail: str | None = None,
) -> SnapshotFrame:
    # Snapshot construction lives once in ``provide.uterm.frames``; this thin
    # wrapper only re-types the result as the local ``SnapshotFrame`` TypedDict
    # so server-side callers and signatures don't churn. The core builder
    # already preserves ``prompt_detected: None`` on the wire (exclude_none=False).
    return cast(
        "SnapshotFrame",
        _core_make_snapshot_frame(
            screen=screen,
            cursor=cursor,
            cols=cols,
            rows=rows,
            screen_hash=screen_hash,
            cursor_at_end=cursor_at_end,
            has_trailing_space=has_trailing_space,
            prompt_detected=prompt_detected,
            ts=ts,
            raw_tail=raw_tail,
        ),
    )


def make_analysis_frame(*, formatted: str, raw: Any, ts: float | None = None) -> AnalysisFrame:
    # ``raw`` is allowed to be None and must survive the dump — the
    # frontend reads it directly. Use exclude_none=False so a None ``raw``
    # is still serialised as ``"raw": null`` to match the legacy output.
    return cast(
        "AnalysisFrame",
        _AnalysisModel(
            type="analysis",
            formatted=formatted,
            raw=raw,
            ts=time.time() if ts is None else ts,
        ).model_dump(exclude_none=False),
    )


def make_hijack_state_frame(
    *,
    hijacked: bool,
    owner: str | None,
    lease_expires_at: float | None,
    input_mode: str,
) -> HijackStateFrame:
    # exclude_none=False — owner / lease_expires_at can legitimately be None
    # and the frontend reads them directly off the frame.
    return cast(
        "HijackStateFrame",
        _HijackStateModel(
            type="hijack_state",
            hijacked=hijacked,
            owner=owner,
            lease_expires_at=lease_expires_at,
            input_mode=input_mode,
        ).model_dump(exclude_none=False),
    )


def make_hello_frame(**payload: Any) -> HelloFrame:
    # Hello-frame payloads accept arbitrary capability flags that aren't
    # part of the Pydantic schema (``HelloFrame`` uses extra="ignore"),
    # so route through the loose TypedDict path and just stamp ``type``.
    return cast("HelloFrame", {"type": "hello", **payload})


def coerce_worker_status_frame(payload: dict[str, Any]) -> WorkerStatusFrame:
    frame = dict(payload)
    frame.setdefault("type", "status")
    frame.setdefault("ts", time.time())
    return cast("WorkerStatusFrame", frame)
