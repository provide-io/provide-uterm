#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request

if TYPE_CHECKING:
    from collections.abc import Callable

    from provide.uterm.server.authorization import AuthorizationService


class ApprovalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


@dataclass
class ApprovalRequest:
    id: str
    worker_id: str
    submitter_id: str
    command: str
    status: ApprovalStatus
    created_at: float
    expires_at: float
    group_id: str | None = None
    is_fanout: bool = False


class InMemoryApprovalStore:
    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}
        self.on_expired: Callable[[str], Any] | None = None

    def add(self, request: ApprovalRequest) -> None:
        self._requests[request.id] = request

    def get(self, request_id: str) -> ApprovalRequest | None:
        return self._requests.get(request_id)

    def resolve(self, request_id: str, status: ApprovalStatus) -> None:
        req = self.get(request_id)
        if req and req.status == ApprovalStatus.PENDING:
            req.status = status

    async def cleanup_expired(self) -> None:
        now = time.time()
        # Prune entries that have been in a terminal state (APPROVED, REJECTED, TIMEOUT)
        # for more than 1 hour beyond their expiration time.
        PRUNE_TTL = 3600

        for req_id, req in list(self._requests.items()):
            if req.status == ApprovalStatus.PENDING and req.expires_at < now:
                req.status = ApprovalStatus.TIMEOUT
                if self.on_expired:
                    # Notify subscribers (e.g. FanOutController) to prune state
                    res = self.on_expired(req.id)
                    if asyncio.iscoroutine(res):
                        await res
            elif req.status != ApprovalStatus.PENDING and (req.expires_at + PRUNE_TTL) < now:
                del self._requests[req_id]


def create_approvals_router() -> APIRouter:
    from typing import cast

    from provide.uterm.bridge.hub.ext import PolicyDecision

    router = APIRouter(prefix="/api/approvals", tags=["approvals"])

    @router.get("")
    async def list_approvals(request: Request) -> list[dict[str, Any]]:
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
            for req in hub._approval_store._requests.values()
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
        approval_req = hub._approval_store.get(request_id)
        if not approval_req:
            raise HTTPException(status_code=404, detail="Approval request not found")

        if approval_req.status != ApprovalStatus.PENDING:
            raise HTTPException(status_code=400, detail="Approval request is not pending")

        await hub.resolve_approval(
            approval_req.worker_id, request_id, PolicyDecision(action="allow"), approval_req.command
        )
        hub._approval_store.resolve(request_id, ApprovalStatus.APPROVED)
        return {"status": "approved"}

    @router.post("/{request_id}/reject")
    async def reject_command(request_id: str, request: Request, reason: str | None = None) -> dict[str, str]:
        await _require_admin(request)
        hub = request.app.state.uterm_hub
        approval_req = hub._approval_store.get(request_id)
        if not approval_req:
            raise HTTPException(status_code=404, detail="Approval request not found")

        if approval_req.status != ApprovalStatus.PENDING:
            raise HTTPException(status_code=400, detail="Approval request is not pending")

        await hub.resolve_approval(
            approval_req.worker_id, request_id, PolicyDecision(action="deny", reason=reason), approval_req.command
        )
        hub._approval_store.resolve(request_id, ApprovalStatus.REJECTED)
        return {"status": "rejected"}

    return router
