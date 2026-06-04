#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Mutation-enforced at killed==100 ([tool.mutmut]); bound suite: tests/server/test_routes_mutation_killing.py (router-endpoint extraction, mocked Request).
from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, HTTPException, Request

from provide.uterm.server.bridge.hub.approvals import ApprovalStatus
from provide.uterm.server.bridge.hub.ext import PolicyDecision

if TYPE_CHECKING:
    from provide.uterm.server.authorization import AuthorizationService


def create_approvals_router() -> APIRouter:
    router = APIRouter(prefix="/api/approvals", tags=["approvals"])

    @router.get("")
    async def list_approvals(request: Request) -> list[dict[str, Any]]:
        await _require_admin(request)
        hub = request.app.state.uterm_hub
        return [
            {
                "id": req.id,
                "worker_id": req.worker_id,
                "group_id": getattr(req, "group_id", None),
                "is_fanout": getattr(req, "is_fanout", False),
                "submitter_id": req.submitter_id,
                "command": req.command,
                "status": req.status.value,
                "created_at": req.created_at,
                "expires_at": req.expires_at,
            }
            for req in hub.approval_store._requests.values()
            if req.status == ApprovalStatus.PENDING
        ]

    async def _require_admin(request: Request) -> None:
        principal = getattr(request.state, "uterm_principal", None)
        if not principal:
            raise HTTPException(status_code=401, detail="Authentication required")

        authz = cast("AuthorizationService", request.app.state.uterm_authz)
        if not await authz.is_admin(principal):
            raise HTTPException(status_code=403, detail="Admin role required")

    @router.post("/{request_id}/approve")
    async def approve_command(request_id: str, request: Request) -> dict[str, str]:
        await _require_admin(request)
        hub = request.app.state.uterm_hub
        approval_req = hub.approval_store.get(request_id)
        if not approval_req:
            raise HTTPException(status_code=404, detail="Approval request not found")

        if not hub.approval_store.claim(request_id, ApprovalStatus.APPROVED):
            raise HTTPException(status_code=400, detail="Approval request is not pending")
        await hub.resolve_approval(
            approval_req.worker_id, request_id, PolicyDecision(action="allow"), approval_req.command
        )
        return {"status": "approved"}

    @router.post("/{request_id}/reject")
    async def reject_command(request_id: str, request: Request, reason: str | None = None) -> dict[str, str]:
        await _require_admin(request)
        hub = request.app.state.uterm_hub
        approval_req = hub.approval_store.get(request_id)
        if not approval_req:
            raise HTTPException(status_code=404, detail="Approval request not found")

        if not hub.approval_store.claim(request_id, ApprovalStatus.REJECTED):
            raise HTTPException(status_code=400, detail="Approval request is not pending")
        await hub.resolve_approval(
            approval_req.worker_id, request_id, PolicyDecision(action="deny", reason=reason), approval_req.command
        )
        return {"status": "rejected"}

    return router
