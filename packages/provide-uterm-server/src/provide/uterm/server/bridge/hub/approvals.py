#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


class ApprovalStatus(Enum):
    PENDING = "pending"
    RESOLVING = "resolving"
    APPROVED = "approved"
    REJECTED = "rejected"
    REFUSED = "refused"
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
    # Exact browser ownership epoch that submitted this held command. Fan-out
    # approvals intentionally leave these unset because they have no terminal
    # input owner.
    origin_browser: Any | None = None
    ownership_generation: int | None = None
    # Store-assigned monotonic identity. Completion must present the revision it
    # claimed so a pruned/reused request ID cannot receive a stale status write.
    revision: int = 0


class InMemoryApprovalStore:
    """In-memory store for approval requests.

    Uses a ``threading.Lock`` to make the check-then-set ``resolve`` and the
    iteration in ``cleanup_expired`` safe against concurrent mutation. The
    critical sections are microsecond-scale dictionary operations; taking a
    threading lock from asyncio code does not block any meaningful work.

    Records are copied at the boundary. Callers never receive the live mutable
    object held by the store, and duplicate IDs are rejected rather than
    replacing an in-flight approval.
    """

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}
        self._lock = threading.Lock()
        self._next_revision = 0
        self.on_expired: Callable[[str], Any] | None = None

    def add(self, request: ApprovalRequest) -> bool:
        with self._lock:
            if request.id in self._requests:
                return False
            self._next_revision += 1
            self._requests[request.id] = replace(request, revision=self._next_revision)
            return True

    def get(self, request_id: str) -> ApprovalRequest | None:
        with self._lock:
            req = self._requests.get(request_id)
            return None if req is None else replace(req)

    def pending(self) -> list[ApprovalRequest]:
        """Return immutable snapshots of every pending request."""
        with self._lock:
            return [replace(req) for req in self._requests.values() if req.status == ApprovalStatus.PENDING]

    def resolve(self, request_id: str, status: ApprovalStatus, *, expected_revision: int) -> bool:
        """Transition a PENDING request to *status*.

        Superseded by ``claim()`` for request handling; retained for
        direct/test use.
        """
        with self._lock:
            req = self._requests.get(request_id)
            if req is None or req.status != ApprovalStatus.PENDING or req.revision != expected_revision:
                return False
            req.status = status
            return True

    def claim(self, request_id: str, status: ApprovalStatus, *, expected_revision: int) -> bool:
        """Atomically transition a PENDING request to *status*.

        Returns ``True`` only for the caller that performs the transition, so a
        held command is resolved — and therefore injected — exactly once even
        under concurrent approve/reject requests. Callers MUST inject the
        command only when this returns ``True``.
        """
        with self._lock:
            req = self._requests.get(request_id)
            if req is None or req.status != ApprovalStatus.PENDING or req.revision != expected_revision:
                return False
            req.status = status
            return True

    def claim_request(
        self, request_id: str, status: ApprovalStatus, *, expected_revision: int
    ) -> ApprovalRequest | None:
        """Claim one exact revision and return its immutable snapshot."""
        with self._lock:
            req = self._requests.get(request_id)
            if req is None or req.status != ApprovalStatus.PENDING or req.revision != expected_revision:
                return None
            req.status = status
            return replace(req)

    def finalize(self, request_id: str, status: ApprovalStatus, *, expected_revision: int) -> bool:
        """Atomically finalize a request currently reserved for resolution."""
        if status not in {ApprovalStatus.APPROVED, ApprovalStatus.REFUSED}:
            raise ValueError("approval resolution must finalize as approved or refused")
        with self._lock:
            req = self._requests.get(request_id)
            if req is None or req.status != ApprovalStatus.RESOLVING or req.revision != expected_revision:
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
