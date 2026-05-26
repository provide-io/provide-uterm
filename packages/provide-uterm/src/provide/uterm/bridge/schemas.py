#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Pydantic v2 frame schemas — single source of truth for WebSocket frames.

These models are the canonical wire-format definition for the terminal bridge
control/data channel. They are:

  * Imported by Python producers (``provide-uterm-server`` / ``cloudflare``)
    via the builder helpers in ``frames.py``.
  * Consumed by the TypeScript frontend via JSON-Schema codegen
    (see ``scripts/codegen_frames.py``). The generated file lives at
    ``packages/provide-uterm-frontend/src/generated/frames.ts``.

The ``AnyFrame`` discriminated union (key = ``type``) covers every frame the
bridge emits or accepts. Adding a new frame type means: define a new model
here, add it to ``AnyFrame``, run ``python scripts/codegen_frames.py``, and
commit the regenerated TS.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class _FrameBase(BaseModel):
    """Base for all frame models — forbid unknown fields so producers and
    consumers can't silently drift."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Terminal data + snapshot
# ---------------------------------------------------------------------------


class TermFrame(_FrameBase):
    """Raw terminal output bytes from the worker to subscribers."""

    type: Literal["term"]
    data: str
    ts: float | None = None


class InputFrame(_FrameBase):
    """Browser/operator input destined for the worker."""

    type: Literal["input"]
    data: str
    ts: float | None = None


class SnapshotReqFrame(_FrameBase):
    """Browser-originated request for a fresh screen snapshot."""

    type: Literal["snapshot_req"]
    ts: float | None = None


class SnapshotFrame(_FrameBase):
    """Worker-originated full-screen snapshot."""

    type: Literal["snapshot"]
    screen: str
    cursor: dict[str, int] | None = None
    cols: int | None = None
    rows: int | None = None
    screen_hash: str | None = None
    cursor_at_end: bool | None = None
    has_trailing_space: bool | None = None
    prompt_detected: dict[str, Any] | None = None
    ts: float | None = None


# ---------------------------------------------------------------------------
# Hijack lease lifecycle
# ---------------------------------------------------------------------------


class ControlFrame(_FrameBase):
    """Server-originated worker-control frame (pause/resume/step)."""

    type: Literal["control"]
    action: str
    owner: str | None = None
    lease_s: float | None = None
    ts: float | None = None


class HijackStateFrame(_FrameBase):
    """Broadcast lease-state update."""

    type: Literal["hijack_state"]
    hijacked: bool
    owner: str | None = None
    lease_expires_at: float | None = None
    input_mode: str | None = None


class HijackRequestFrame(_FrameBase):
    """Browser-originated request to acquire the hijack lease."""

    type: Literal["hijack_request"]
    token: str | None = None
    ts: float | None = None


class HijackReleaseFrame(_FrameBase):
    """Browser-originated request to release the hijack lease."""

    type: Literal["hijack_release"]
    ts: float | None = None


class HijackStepFrame(_FrameBase):
    """Browser-originated single-step request."""

    type: Literal["hijack_step"]
    ts: float | None = None


# ---------------------------------------------------------------------------
# Worker presence
# ---------------------------------------------------------------------------


class WorkerConnectedFrame(_FrameBase):
    type: Literal["worker_connected"]
    worker_id: str
    ts: float | None = None


class WorkerDisconnectedFrame(_FrameBase):
    type: Literal["worker_disconnected"]
    worker_id: str
    ts: float | None = None


class WorkerHelloFrame(_FrameBase):
    """Worker-originated hello-frame carrying input_mode + capabilities."""

    type: Literal["worker_hello"]
    mode: str | None = None
    ts: float | None = None


# ---------------------------------------------------------------------------
# Heartbeat / keepalive
# ---------------------------------------------------------------------------


class HeartbeatFrame(_FrameBase):
    type: Literal["heartbeat"]
    ts: float | None = None


class HeartbeatAckFrame(_FrameBase):
    """Server reply to a browser heartbeat — refreshes the lease."""

    type: Literal["heartbeat_ack"]
    lease_expires_at: float
    ts: float | None = None


class PingFrame(_FrameBase):
    type: Literal["ping"]
    ts: float | None = None


class PongFrame(_FrameBase):
    type: Literal["pong"]
    ts: float | None = None


# ---------------------------------------------------------------------------
# Hello / resume handshake
# ---------------------------------------------------------------------------


class HelloFrame(_FrameBase):
    """Server-originated hello-frame to the browser describing capabilities.

    Schema is intentionally permissive (``extra="ignore"``) because the field
    set drifts as new capabilities land; field-by-field tightening will happen
    once the wire format is fully stable.
    """

    # Allow extra capability flags without breaking forward compatibility.
    model_config = ConfigDict(extra="ignore")

    type: Literal["hello"]
    worker_id: str | None = None
    can_hijack: bool | None = None
    hijacked: bool | None = None
    hijacked_by_me: bool | None = None
    worker_online: bool | None = None
    input_mode: str | None = None
    role: str | None = None
    hijack_control: str | None = None
    hijack_step_supported: bool | None = None
    capabilities: dict[str, Any] | None = None
    resume_supported: bool | None = None
    resume_token: str | None = None
    resumed: bool | None = None
    protocol_version: int | None = None
    protocol: dict[str, int] | None = None
    ts: float | None = None


class ResumeFrame(_FrameBase):
    type: Literal["resume"]
    token: str
    player_id: int | None = None


# ---------------------------------------------------------------------------
# Misc server → browser
# ---------------------------------------------------------------------------


class AnalysisFrame(_FrameBase):
    type: Literal["analysis"]
    formatted: str
    raw: Any | None = None
    ts: float | None = None


class ErrorFrame(_FrameBase):
    type: Literal["error"]
    message: str
    # Protocol-mismatch close frames carry these extras (see contracts.py).
    reason: str | None = None
    client_min: int | None = None
    client_max: int | None = None
    server_min: int | None = None
    server_max: int | None = None


class StatusFrame(_FrameBase):
    """Worker-originated status passthrough (``coerce_worker_status_frame``).

    Schema is permissive because the worker may attach arbitrary status
    payloads. The frame type discriminator and ``ts`` field are the only
    guarantees.
    """

    model_config = ConfigDict(extra="allow")

    type: Literal["status"]
    ts: float | None = None


class InputModeChangedFrame(_FrameBase):
    type: Literal["input_mode_changed"]
    input_mode: str
    ts: float | None = None


# ---------------------------------------------------------------------------
# Approval gating
# ---------------------------------------------------------------------------


class ApprovalPendingFrame(_FrameBase):
    type: Literal["approval_pending"]
    command: str
    request_id: str
    expires_at: float


class ApprovalResolvedFrame(_FrameBase):
    type: Literal["approval_resolved"]
    outcome: str
    request_id: str


# ---------------------------------------------------------------------------
# Presence (DeckMux wire format)
# ---------------------------------------------------------------------------


class PresenceUpdateFrame(_FrameBase):
    """DeckMux per-user presence update — schema is permissive because
    optional fields (scroll, selection, pin, typing, queued_keys) are
    attached only when relevant."""

    model_config = ConfigDict(extra="allow")

    type: Literal["presence_update"]
    user_id: str | None = None


class PresenceSyncFrame(_FrameBase):
    """Full presence roster sent on browser connect."""

    model_config = ConfigDict(extra="allow")

    type: Literal["presence_sync"]
    users: list[dict[str, Any]] | None = None
    config: dict[str, Any] | None = None
    owner_id: str | None = None


class PresenceLeaveFrame(_FrameBase):
    type: Literal["presence_leave"]
    user_id: str
    ts: float | None = None


class ControlTransferFrame(_FrameBase):
    """DeckMux ownership-transfer notice."""

    type: Literal["control_transfer"]
    from_user_id: str | None = None
    to_user_id: str | None = None
    reason: str | None = None
    queued_keys: str | None = None


# ---------------------------------------------------------------------------
# Discriminated union — single entry point
# ---------------------------------------------------------------------------


AnyFrame = Annotated[
    TermFrame
    | InputFrame
    | SnapshotReqFrame
    | SnapshotFrame
    | ControlFrame
    | HijackStateFrame
    | HijackRequestFrame
    | HijackReleaseFrame
    | HijackStepFrame
    | WorkerConnectedFrame
    | WorkerDisconnectedFrame
    | WorkerHelloFrame
    | HeartbeatFrame
    | HeartbeatAckFrame
    | PingFrame
    | PongFrame
    | HelloFrame
    | ResumeFrame
    | AnalysisFrame
    | ErrorFrame
    | StatusFrame
    | InputModeChangedFrame
    | ApprovalPendingFrame
    | ApprovalResolvedFrame
    | PresenceUpdateFrame
    | PresenceSyncFrame
    | PresenceLeaveFrame
    | ControlTransferFrame,
    Field(discriminator="type"),
]


__all__ = [
    "AnalysisFrame",
    "AnyFrame",
    "ApprovalPendingFrame",
    "ApprovalResolvedFrame",
    "ControlFrame",
    "ControlTransferFrame",
    "ErrorFrame",
    "HeartbeatAckFrame",
    "HeartbeatFrame",
    "HelloFrame",
    "HijackReleaseFrame",
    "HijackRequestFrame",
    "HijackStateFrame",
    "HijackStepFrame",
    "InputFrame",
    "InputModeChangedFrame",
    "PingFrame",
    "PongFrame",
    "PresenceLeaveFrame",
    "PresenceSyncFrame",
    "PresenceUpdateFrame",
    "ResumeFrame",
    "SnapshotFrame",
    "SnapshotReqFrame",
    "StatusFrame",
    "TermFrame",
    "WorkerConnectedFrame",
    "WorkerDisconnectedFrame",
    "WorkerHelloFrame",
]
