#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared REST API contract definitions for the terminal bridge.

These TypedDicts define the canonical schema for REST responses and are shared
between the FastAPI backend (provide-uterm-server) and the Cloudflare
Durable Objects implementation (provide-uterm-cloudflare).
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

# ---------------------------------------------------------------------------
# Common Enums and Literals
# ---------------------------------------------------------------------------

SessionLifecycle = Literal["stopped", "starting", "running", "error"]
InputMode = Literal["hijack", "open"]
Visibility = Literal["public", "operator", "private"]

# Protocol version range carried in the hello-frame handshake.
#
# Peers advertise ``{"min": MIN, "max": MAX, "preferred": PREFERRED}`` in
# their hello frame. The server intersects the client range against
# its own and picks the highest mutually-supported version, or closes
# the WebSocket with code 1002 + an error frame if there's no overlap.
#
# Lockstep is preserved while only one version exists (min == max == 1).
# When a new protocol version lands, bump MAX_PROTOCOL_VERSION first,
# leave MIN at the oldest still-supported version, and set PREFERRED to
# whatever the server should actively pick during negotiation. See
# ``.provide/design/protocol-version-handshake.md``.
MIN_PROTOCOL_VERSION = 1
MAX_PROTOCOL_VERSION = 1
PREFERRED_PROTOCOL_VERSION = 1

# Backward-compatible alias for existing callers that just want "the
# current version" (typically for stamping outbound frames). New code
# should reference the range fields above.
CURRENT_PROTOCOL_VERSION = PREFERRED_PROTOCOL_VERSION


def negotiate_protocol_version(client_min: int, client_max: int) -> int | None:
    """Return the version both sides should use, or None on no overlap.

    The chosen version is the highest of ``[server_min..server_max]
    intersect [client_min..client_max]``. ``None`` means the handshake
    must fail and the caller should close 1002.
    """
    lo = max(int(client_min), MIN_PROTOCOL_VERSION)
    hi = min(int(client_max), MAX_PROTOCOL_VERSION)
    if lo > hi:
        return None
    return hi


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

    Mirrors provide-uterm-server SessionRuntimeStatus.
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
    # Hello-frame range negotiation. Senders include ``protocol``; the
    # server reply additionally sets ``protocol.selected`` to the picked
    # version after intersection. See negotiate_protocol_version().
    protocol: dict[str, int]
    # Error frames emitted on protocol-mismatch close (code 1002) carry
    # ``reason="protocol_mismatch"`` plus the offending min/max pair so
    # the client can surface a useful disconnect message.
    reason: str
    client_min: int
    client_max: int
    server_min: int
    server_max: int
