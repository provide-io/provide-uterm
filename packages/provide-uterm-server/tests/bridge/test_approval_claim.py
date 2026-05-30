#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from provide.uterm.server.bridge.hub.approvals import (
    ApprovalRequest,
    ApprovalStatus,
    InMemoryApprovalStore,
)


def _pending(req_id: str = "r1") -> ApprovalRequest:
    return ApprovalRequest(
        id=req_id,
        worker_id="w1",
        submitter_id="s1",
        command="rm -rf /",
        status=ApprovalStatus.PENDING,
        created_at=0.0,
        expires_at=1e12,
    )


def test_claim_succeeds_exactly_once() -> None:
    store = InMemoryApprovalStore()
    store.add(_pending())
    assert store.claim("r1", ApprovalStatus.APPROVED) is True
    assert store.claim("r1", ApprovalStatus.REJECTED) is False
    assert store.get("r1").status == ApprovalStatus.APPROVED


def test_claim_missing_request_returns_false() -> None:
    store = InMemoryApprovalStore()
    assert store.claim("nope", ApprovalStatus.APPROVED) is False
