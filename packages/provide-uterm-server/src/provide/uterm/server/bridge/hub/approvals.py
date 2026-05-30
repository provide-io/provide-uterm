#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import asyncio
import threading
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
    """In-memory store for approval requests.

    Uses a ``threading.Lock`` to make the check-then-set ``resolve`` and the
    iteration in ``cleanup_expired`` safe against concurrent mutation. The
    critical sections are microsecond-scale dictionary operations; taking a
    threading lock from asyncio code does not block any meaningful work.

    Note: ``get`` is intentionally lock-free since a single dict read is atomic
    in CPython. Callers MUST NOT iterate the live ``_requests`` dict without
    holding ``_lock`` themselves.
    """

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}
        self._lock = threading.Lock()
        self.on_expired: Callable[[str], Any] | None = None

    def add(self, request: ApprovalRequest) -> None:
        with self._lock:
            self._requests[request.id] = request

    def get(self, request_id: str) -> ApprovalRequest | None:
        return self._requests.get(request_id)

    def resolve(self, request_id: str, status: ApprovalStatus) -> None:
        """Transition a PENDING request to *status*.

        Superseded by ``claim()`` for request handling; retained for
        direct/test use.
        """
        with self._lock:
            req = self._requests.get(request_id)
            if req and req.status == ApprovalStatus.PENDING:
                req.status = status

    def claim(self, request_id: str, status: ApprovalStatus) -> bool:
        """Atomically transition a PENDING request to *status*.

        Returns ``True`` only for the caller that performs the transition, so a
        held command is resolved — and therefore injected — exactly once even
        under concurrent approve/reject requests. Callers MUST inject the
        command only when this returns ``True``.
        """
        with self._lock:
            req = self._requests.get(request_id)
            if req is None or req.status != ApprovalStatus.PENDING:
                return False
            req.status = status
            return True

    async def cleanup_expired(self) -> None:
        now = time.time()
        # Prune entries that have been in a terminal state (APPROVED, REJECTED, TIMEOUT)
        # for more than 1 hour beyond their expiration time.
        PRUNE_TTL = 3600

        # Hold the lock only while snapshotting + mutating dict state. The
        # ``on_expired`` callback may be async and is invoked outside the lock
        # to avoid blocking other threads on user code.
        expired_ids: list[str] = []
        with self._lock:
            for req_id, req in list(self._requests.items()):
                if req.status == ApprovalStatus.PENDING and req.expires_at < now:
                    req.status = ApprovalStatus.TIMEOUT
                    expired_ids.append(req.id)
                elif req.status != ApprovalStatus.PENDING and (req.expires_at + PRUNE_TTL) < now:
                    del self._requests[req_id]

        if self.on_expired:
            for req_id in expired_ids:
                # Notify subscribers (e.g. FanOutController) to prune state
                res = self.on_expired(req_id)
                if asyncio.iscoroutine(res):
                    await res
