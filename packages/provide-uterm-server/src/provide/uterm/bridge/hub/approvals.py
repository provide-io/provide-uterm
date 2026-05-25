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

if TYPE_CHECKING:
    from collections.abc import Callable


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
