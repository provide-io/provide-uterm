#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared REST API contract definitions for the terminal bridge.

These TypedDicts define the canonical schema for REST responses and are shared
between the FastAPI backend (provide-terminal-server) and the Cloudflare
Durable Objects implementation (provide-terminal-cloudflare).
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

# ---------------------------------------------------------------------------
# Common Enums and Literals
# ---------------------------------------------------------------------------

SessionLifecycle = Literal["stopped", "starting", "running", "error"]
InputMode = Literal["hijack", "open"]
Visibility = Literal["public", "operator", "private"]

CURRENT_PROTOCOL_VERSION = 1

# ---------------------------------------------------------------------------
# REST API Response Contracts
# ---------------------------------------------------------------------------


class HijackAcquireResponse(TypedDict):
    ok: bool
    worker_id: str
    hijack_id: str
    lease_expires_at: float
    owner: str


class HijackHeartbeatResponse(TypedDict):
    ok: bool
    worker_id: str
    hijack_id: str
    lease_expires_at: float


class HijackStepResponse(TypedDict):
    ok: bool
    worker_id: str
    hijack_id: str
    lease_expires_at: float | None


class HijackReleaseResponse(TypedDict):
    ok: bool
    worker_id: str
    hijack_id: str


class HijackSnapshotResponse(TypedDict):
    ok: bool
    worker_id: str
    hijack_id: str
    snapshot: dict[str, object] | None
    prompt_id: str | None
    lease_expires_at: float | None


class HijackSendResponse(TypedDict):
    ok: bool
    worker_id: str
    hijack_id: str
    sent: str
    matched_prompt_id: str | None
    lease_expires_at: float | None


class HijackEventsResponse(TypedDict):
    ok: bool
    worker_id: str
    hijack_id: str
    after_seq: int
    latest_seq: int
    min_event_seq: int
    has_more: bool
    events: list[dict[str, Any]]
    lease_expires_at: float | None


class SessionStatusResponse(TypedDict, total=False):
    """Shape of GET /api/sessions/{id} and items in GET /api/sessions.

    Mirrors provide-terminal-server SessionRuntimeStatus.
    """

    session_id: str
    display_name: str
    created_at: float
    connector_type: str
    lifecycle_state: str
    input_mode: str
    connected: bool
    auto_start: bool
    tags: list[str]
    recording_enabled: bool
    recording_available: bool
    owner: str | None
    visibility: str
    last_error: str | None
    # Backend-specific extras (clients must tolerate them)
    hijacked: bool


class SessionSnapshotResponse(TypedDict):
    """Shape of GET /api/sessions/{id}/snapshot response."""

    session_id: str
    snapshot: dict[str, Any] | None
    prompt_detected: dict[str, Any] | None
    prompt_id: str | None


class SessionEventsResponse(TypedDict):
    """Shape of GET /api/sessions/{id}/events response."""

    session_id: str
    after_seq: int
    latest_seq: int
    min_event_seq: int
    has_more: bool
    events: list[dict[str, Any]]


class SessionModeResponse(TypedDict):
    """Shape of POST /api/sessions/{id}/mode response."""

    ok: bool
    input_mode: str
    worker_id: str


class SessionAnalyzeResponse(TypedDict):
    """Shape of POST /api/sessions/{id}/analyze response."""

    ok: bool
    analysis: str | None
    worker_id: str


class RecordingMetaResponse(TypedDict):
    """Shape of GET /api/sessions/{id}/recording response."""

    session_id: str
    enabled: bool
    entry_count: int
    exists: bool


class RecordingEntry(TypedDict):
    """Single entry from GET /api/sessions/{id}/recording/entries."""

    ts: float
    event: str
    data: dict[str, Any]


# ---------------------------------------------------------------------------
# WebSocket Protocol Frames
# ---------------------------------------------------------------------------

FrameType = Literal[
    "snapshot_req",
    "snapshot",
    "term",
    "input",
    "control",
    "hijack_state",
    "analysis",
    "error",
    "worker_connected",
    "worker_disconnected",
    # Worker-originated lifecycle frame carrying input_mode.
    "worker_hello",
    # Browser-originated frames (heartbeat/ping keepalives, WS-level hijack requests).
    "heartbeat",
    "ping",
    "hijack_request",
    "hijack_release",
    "hijack_step",
    "hello",
    "resume",
]


class Frame(TypedDict, total=False):
    """Canonical shape of a control-channel or data-channel frame."""

    type: FrameType
    ts: float
    data: str
    screen: str
    action: str
    owner: str | None
    hijacked: bool
    lease_expires_at: float | None
    formatted: str
    message: str
    mode: str  # worker_hello: input_mode value ("hijack" or "open")
    token: str
    protocol_version: int
