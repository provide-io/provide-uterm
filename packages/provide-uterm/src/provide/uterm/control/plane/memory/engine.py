#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from provide.uterm.control.plane.memory.approval_store import MemoryApprovalStore
from provide.uterm.control.plane.memory.graphical_target_store import MemoryGraphicalTargetStore
from provide.uterm.control.plane.memory.lease_store import MemoryLeaseStore
from provide.uterm.control.plane.memory.session_store import MemorySessionStore
from provide.uterm.control.plane.memory.token_store import MemoryTokenStore
from provide.uterm.control.plane.memory.transaction import MemoryState, MemoryTransaction

if TYPE_CHECKING:
    from provide.uterm.control.plane.capability import EngineCapabilities
    from provide.uterm.control.plane.types import ControlPlaneConfig


@dataclass(slots=True)
class MemoryControlPlane:
    """In-memory control-plane backend with shared mutable state."""

    config: ControlPlaneConfig
    capabilities: EngineCapabilities = field(init=False)
    _state: MemoryState = field(init=False, repr=False)
    _lock: asyncio.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.capabilities = self.config.capabilities
        self._state = MemoryState()
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def migrate(self) -> None:
        return None

    async def begin(self) -> MemoryTransaction:
        return MemoryTransaction(self._state, self._lock)

    async def reap(self, *, now: float, retention_s: int) -> int:
        """Physically drop control-plane rows whose soft-delete/expiry timestamp
        is older than ``now - retention_s``.  Mirrors the SQLite reap predicate
        semantics (strict ``<``, IS-NOT-NULL guards) so both backends prune the
        same rows.  Returns the total number of records removed."""
        cutoff = now - retention_s
        async with self._lock:
            s = self._state
            before = len(s.resume_tokens) + len(s.session_tokens) + len(s.sessions) + len(s.leases) + len(s.approvals)
            s.resume_tokens = {
                k: r
                for k, r in s.resume_tokens.items()
                if not ((r.revoked_at is not None and r.revoked_at < cutoff) or r.expires_at < cutoff)
            }
            s.session_tokens = {
                k: r
                for k, r in s.session_tokens.items()
                if not (
                    (r.revoked_at is not None and r.revoked_at < cutoff)
                    or (r.expires_at is not None and r.expires_at < cutoff)
                )
            }
            s.sessions = {
                k: r for k, r in s.sessions.items() if not (r.deleted_at is not None and r.deleted_at < cutoff)
            }
            s.leases = {
                k: r
                for k, r in s.leases.items()
                if not ((r.deleted_at is not None and r.deleted_at < cutoff) or r.lease_expires_at < cutoff)
            }
            s.approvals = {
                k: r for k, r in s.approvals.items() if not (r.resolved_at is not None and r.resolved_at < cutoff)
            }
            after = len(s.resume_tokens) + len(s.session_tokens) + len(s.sessions) + len(s.leases) + len(s.approvals)
            return before - after

    async def get_audit_head(self) -> tuple[int, str] | None:
        """Return the in-memory audit-chain head ``(seq, record_hash)``, or
        ``None`` if none recorded yet.  NON-DURABLE: the memory backend loses the
        head on restart (consistent with this backend's documented volatility),
        so cross-restart anti-rollback only holds for the sqlite backend."""
        async with self._lock:
            return self._state.audit_head

    async def set_audit_head(self, seq: int, record_hash: str) -> None:
        """Persist the audit-chain head MONOTONICALLY.

        A lower-or-equal seq is a NO-OP, so the head never moves backwards
        (anti-rollback guard).  NON-DURABLE — see ``get_audit_head``."""
        async with self._lock:
            if self._state.audit_head is not None and self._state.audit_head[0] >= seq:
                return
            self._state.audit_head = (seq, record_hash)

    def session_store(self, tx: MemoryTransaction) -> MemorySessionStore:
        return MemorySessionStore(tx.state, tx)

    def token_store(self, tx: MemoryTransaction) -> MemoryTokenStore:
        return MemoryTokenStore(tx.state, tx)

    def approval_store(self, tx: MemoryTransaction) -> MemoryApprovalStore:
        return MemoryApprovalStore(tx.state, tx)

    def lease_store(self, tx: MemoryTransaction) -> MemoryLeaseStore:
        return MemoryLeaseStore(tx.state, tx)

    def graphical_target_store(self, tx: Any) -> MemoryGraphicalTargetStore:
        return MemoryGraphicalTargetStore(tx.state, tx)
