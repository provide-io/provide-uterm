#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import pytest

from provide.uterm.control.plane import ControlPlaneBackend, ControlPlaneConfig, bootstrap_control_plane
from provide.uterm.control.plane.approval.types import ApprovalRecord
from provide.uterm.control.plane.lease.types import LeaseRecord
from provide.uterm.control.plane.memory import MemoryControlPlane
from provide.uterm.control.plane.session.types import SessionRecord
from provide.uterm.control.plane.token.types import ResumeTokenRecord, SessionTokenRecord

# A fixed "now" so retention math is deterministic.  retention_s=100 => cutoff=900.
NOW = 1000.0
RETENTION_S = 100
CUTOFF = NOW - RETENTION_S  # 900.0


@pytest.mark.asyncio
async def test_bootstrap_control_plane_selects_memory_backend() -> None:
    backend: ControlPlaneBackend = "memory"
    plane = await bootstrap_control_plane(ControlPlaneConfig(backend=backend))

    assert plane.__class__.__name__ == "MemoryControlPlane"


def _resume(token_value: str, *, expires_at: float, revoked_at: float | None = None) -> ResumeTokenRecord:
    return ResumeTokenRecord(
        token_value=token_value,
        session_id="s1",
        role="viewer",
        created_at=0.0,
        expires_at=expires_at,
        revoked_at=revoked_at,
    )


def _session_token(session_id: str, *, expires_at: float | None, revoked_at: float | None = None) -> SessionTokenRecord:
    return SessionTokenRecord(
        session_id=session_id,
        token_kind="operator",
        token_value=f"v-{session_id}",
        created_at=0.0,
        expires_at=expires_at,
        revoked_at=revoked_at,
    )


def _session(session_id: str, *, deleted_at: float | None) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        display_name=session_id,
        connector_type="shell",
        owner=None,
        visibility="private",
        lifecycle_state="stopped" if deleted_at is not None else "running",
        created_at=0.0,
        updated_at=0.0,
        deleted_at=deleted_at,
    )


def _lease(session_id: str, *, lease_expires_at: float, deleted_at: float | None = None) -> LeaseRecord:
    return LeaseRecord(
        session_id=session_id,
        hijack_id=f"h-{session_id}",
        owner="alice",
        lease_expires_at=lease_expires_at,
        created_at=0.0,
        deleted_at=deleted_at,
    )


def _approval(approval_id: str, *, resolved_at: float | None) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=approval_id,
        session_id="s1",
        command="ls",
        requested_by=None,
        state="approved" if resolved_at is not None else "pending",
        created_at=0.0,
        resolved_at=resolved_at,
    )


@pytest.mark.asyncio
async def test_memory_reap_sweeps_soft_deleted_and_expired_rows() -> None:
    plane = MemoryControlPlane(ControlPlaneConfig(backend="memory"))
    state = plane._state

    # Resume tokens: revoked-past-cutoff -> gone, expired-past-cutoff -> gone, fresh -> survives.
    state.resume_tokens["rev-old"] = _resume("rev-old", expires_at=NOW + 10_000, revoked_at=CUTOFF - 1)
    state.resume_tokens["exp-old"] = _resume("exp-old", expires_at=CUTOFF - 1)
    state.resume_tokens["valid"] = _resume("valid", expires_at=NOW + 10_000)
    # Boundary: revoked exactly at cutoff survives (strict <).
    state.resume_tokens["rev-new"] = _resume("rev-new", expires_at=NOW + 10_000, revoked_at=CUTOFF)

    # Session tokens: expired -> gone, revoked-past -> gone, never-expires -> survives.
    state.session_tokens[("s-exp", "operator")] = _session_token("s-exp", expires_at=CUTOFF - 1)
    state.session_tokens[("s-rev", "operator")] = _session_token("s-rev", expires_at=None, revoked_at=CUTOFF - 1)
    state.session_tokens[("s-live", "operator")] = _session_token("s-live", expires_at=None)

    # Sessions: soft-deleted past cutoff -> gone, live -> survives.
    state.sessions["dead"] = _session("dead", deleted_at=CUTOFF - 1)
    state.sessions["live"] = _session("live", deleted_at=None)

    # Leases: soft-deleted -> gone, expired-by-lease_expires_at -> gone, live -> survives.
    state.leases["lease-dead"] = _lease("lease-dead", lease_expires_at=NOW + 10_000, deleted_at=CUTOFF - 1)
    state.leases["lease-exp"] = _lease("lease-exp", lease_expires_at=CUTOFF - 1)
    state.leases["lease-live"] = _lease("lease-live", lease_expires_at=NOW + 10_000)

    # Approvals: resolved past cutoff -> gone, pending -> survives.
    state.approvals["a-old"] = _approval("a-old", resolved_at=CUTOFF - 1)
    state.approvals["a-pending"] = _approval("a-pending", resolved_at=None)

    deleted = await plane.reap(now=NOW, retention_s=RETENTION_S)

    assert deleted == 8
    assert set(state.resume_tokens) == {"valid", "rev-new"}
    assert set(state.session_tokens) == {("s-live", "operator")}
    assert set(state.sessions) == {"live"}
    assert set(state.leases) == {"lease-live"}
    assert set(state.approvals) == {"a-pending"}


@pytest.mark.asyncio
async def test_memory_reap_returns_zero_when_nothing_to_remove() -> None:
    plane = MemoryControlPlane(ControlPlaneConfig(backend="memory"))
    plane._state.resume_tokens["valid"] = _resume("valid", expires_at=NOW + 10_000)

    assert await plane.reap(now=NOW, retention_s=RETENTION_S) == 0
    assert set(plane._state.resume_tokens) == {"valid"}
