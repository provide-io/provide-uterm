#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Round-trip + validation tests for the Pydantic frame schemas.

These tests guard the single-source-of-truth contract in
``provide.uterm.bridge.schemas``: any payload a Python producer hands to
``encode_control()`` or a worker emits inline must validate against
``AnyFrame``, and any payload the discriminator can't recognise must fail
loudly rather than silently being treated as an opaque dict.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from provide.uterm.bridge.schemas import (
    AnalysisFrame,
    AnyFrame,
    ApprovalPendingFrame,
    ApprovalResolvedFrame,
    ControlFrame,
    ControlTransferFrame,
    ErrorFrame,
    HeartbeatAckFrame,
    HeartbeatFrame,
    HelloFrame,
    HijackReleaseFrame,
    HijackRequestFrame,
    HijackStateFrame,
    HijackStepFrame,
    InputFrame,
    InputModeChangedFrame,
    PingFrame,
    PongFrame,
    PresenceLeaveFrame,
    PresenceSyncFrame,
    PresenceUpdateFrame,
    ResumeFrame,
    SnapshotFrame,
    SnapshotReqFrame,
    StatusFrame,
    TermFrame,
    WorkerConnectedFrame,
    WorkerDisconnectedFrame,
    WorkerHelloFrame,
)

_ADAPTER: TypeAdapter[Any] = TypeAdapter(AnyFrame)


def _roundtrip(model: Any, payload: dict[str, Any]) -> None:
    """Validate that ``payload`` survives a full Pydantic round-trip."""
    instance = model.model_validate(payload)
    dumped = instance.model_dump(exclude_none=True, mode="json")
    # Re-validate through the discriminated union to confirm the discriminator
    # picks the right subclass back out.
    again = _ADAPTER.validate_python(dumped)
    assert type(again) is model


class TestRoundTrip:
    """Each frame type round-trips through ``model_dump`` and ``AnyFrame``."""

    def test_term(self) -> None:
        _roundtrip(TermFrame, {"type": "term", "data": "hello", "ts": 1.0})

    def test_input(self) -> None:
        _roundtrip(InputFrame, {"type": "input", "data": "ls\n"})

    def test_snapshot_req(self) -> None:
        _roundtrip(SnapshotReqFrame, {"type": "snapshot_req"})

    def test_snapshot(self) -> None:
        _roundtrip(
            SnapshotFrame,
            {
                "type": "snapshot",
                "screen": "$ ",
                "cursor": {"row": 0, "col": 2},
                "cols": 80,
                "rows": 24,
                "screen_hash": "abc",
                "cursor_at_end": True,
                "has_trailing_space": True,
                "prompt_detected": None,
                "ts": 1.0,
                "event_seq": 7,
            },
        )

    def test_snapshot_event_sequence_is_optional(self) -> None:
        snapshot = SnapshotFrame.model_validate({"type": "snapshot", "screen": "$ "})
        assert snapshot.event_seq is None

    def test_control(self) -> None:
        _roundtrip(
            ControlFrame,
            {"type": "control", "action": "pause", "owner": "dashboard", "lease_s": 0, "ts": 1.0},
        )

    def test_hijack_state(self) -> None:
        _roundtrip(
            HijackStateFrame,
            {
                "type": "hijack_state",
                "hijacked": True,
                "owner": "alice",
                "lease_expires_at": 12.5,
                "input_mode": "hijack",
            },
        )

    def test_hijack_request(self) -> None:
        _roundtrip(HijackRequestFrame, {"type": "hijack_request"})

    def test_hijack_release(self) -> None:
        _roundtrip(HijackReleaseFrame, {"type": "hijack_release"})

    def test_hijack_step(self) -> None:
        _roundtrip(HijackStepFrame, {"type": "hijack_step"})

    def test_worker_connected(self) -> None:
        _roundtrip(WorkerConnectedFrame, {"type": "worker_connected", "worker_id": "w1", "ts": 1.0})

    def test_worker_disconnected(self) -> None:
        _roundtrip(WorkerDisconnectedFrame, {"type": "worker_disconnected", "worker_id": "w1"})

    def test_worker_hello(self) -> None:
        _roundtrip(WorkerHelloFrame, {"type": "worker_hello", "mode": "open"})

    def test_heartbeat(self) -> None:
        _roundtrip(HeartbeatFrame, {"type": "heartbeat"})

    def test_heartbeat_ack(self) -> None:
        _roundtrip(HeartbeatAckFrame, {"type": "heartbeat_ack", "lease_expires_at": 10.0, "ts": 1.0})

    def test_ping(self) -> None:
        _roundtrip(PingFrame, {"type": "ping"})

    def test_pong(self) -> None:
        _roundtrip(PongFrame, {"type": "pong", "ts": 1.0})

    def test_hello(self) -> None:
        _roundtrip(
            HelloFrame,
            {
                "type": "hello",
                "worker_id": "w1",
                "can_hijack": True,
                "hijacked": False,
                "input_mode": "open",
                "role": "viewer",
                "capabilities": {"hijack_control": "ws"},
                "protocol_version": 1,
                "protocol": {"selected": 1, "server_min": 1, "server_max": 1},
            },
        )

    def test_resume(self) -> None:
        _roundtrip(ResumeFrame, {"type": "resume", "token": "abc"})

    def test_analysis(self) -> None:
        _roundtrip(AnalysisFrame, {"type": "analysis", "formatted": "ok", "raw": {"x": 1}, "ts": 1.0})

    def test_error(self) -> None:
        _roundtrip(ErrorFrame, {"type": "error", "message": "boom"})

    def test_status(self) -> None:
        _roundtrip(StatusFrame, {"type": "status", "ts": 1.0, "label": "running"})

    def test_input_mode_changed(self) -> None:
        _roundtrip(InputModeChangedFrame, {"type": "input_mode_changed", "input_mode": "hijack", "ts": 1.0})

    def test_approval_pending(self) -> None:
        _roundtrip(
            ApprovalPendingFrame,
            {"type": "approval_pending", "command": "rm -rf /", "request_id": "r1", "expires_at": 5.0},
        )

    def test_approval_resolved(self) -> None:
        _roundtrip(
            ApprovalResolvedFrame,
            {"type": "approval_resolved", "outcome": "approved", "request_id": "r1"},
        )

    def test_presence_update(self) -> None:
        _roundtrip(
            PresenceUpdateFrame,
            {"type": "presence_update", "user_id": "u1", "scroll_line": 3},
        )

    def test_presence_sync(self) -> None:
        _roundtrip(
            PresenceSyncFrame,
            {"type": "presence_sync", "users": [{"user_id": "u1"}], "config": {}, "owner_id": "u1"},
        )

    def test_presence_leave(self) -> None:
        _roundtrip(PresenceLeaveFrame, {"type": "presence_leave", "user_id": "u1", "ts": 1.0})

    def test_control_transfer(self) -> None:
        _roundtrip(
            ControlTransferFrame,
            {
                "type": "control_transfer",
                "from_user_id": "u1",
                "to_user_id": "u2",
                "reason": "handover",
                "queued_keys": "",
            },
        )


class TestValidation:
    """Negative paths — unknown types and unexpected fields must raise."""

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _ADAPTER.validate_python({"type": "definitely_not_a_real_frame"})

    def test_extra_field_rejected_on_strict_model(self) -> None:
        # TermFrame uses the default ``extra="forbid"``.
        with pytest.raises(ValidationError):
            TermFrame.model_validate({"type": "term", "data": "hi", "surprise": True})

    def test_missing_required_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _ADAPTER.validate_python({"type": "approval_pending"})  # missing command/request_id/expires_at

    def test_discriminator_narrows(self) -> None:
        """A union-decoded frame is the correct concrete subtype."""
        decoded = _ADAPTER.validate_python({"type": "hijack_state", "hijacked": False})
        assert isinstance(decoded, HijackStateFrame)
        assert decoded.hijacked is False

    def test_status_frame_allows_extras(self) -> None:
        """``status`` intentionally allows extra fields (worker passthrough)."""
        instance = StatusFrame.model_validate({"type": "status", "ts": 1.0, "cpu": 0.5, "queue": 12})
        dumped = instance.model_dump(exclude_none=True, mode="json")
        assert dumped["cpu"] == 0.5
        assert dumped["queue"] == 12

    def test_hello_frame_ignores_extras(self) -> None:
        """``hello`` ignores unknown capability flags rather than failing."""
        instance = HelloFrame.model_validate({"type": "hello", "worker_id": "w1", "future_flag": True})
        dumped = instance.model_dump(exclude_none=True, mode="json")
        assert "future_flag" not in dumped
        assert dumped["worker_id"] == "w1"
