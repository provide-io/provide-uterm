#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from provide.uterm.control.plane.capability import EngineCapabilities
    from provide.uterm.control.plane.transaction import Transaction
    from provide.uterm.control.plane.types import ControlPlaneConfig


class ControlPlane(Protocol):
    capabilities: EngineCapabilities

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def migrate(self) -> None: ...

    async def begin(self) -> Transaction: ...

    async def reap(self, *, now: float, retention_s: int) -> int: ...

    async def get_audit_head(self) -> tuple[int, str] | None:
        """Return the persisted audit-chain head ``(seq, record_hash)``, or
        ``None`` if no head has been recorded yet (genesis)."""
        ...

    async def set_audit_head(self, seq: int, record_hash: str) -> None:
        """Persist the audit-chain head MONOTONICALLY.

        If a head already exists with ``existing_seq >= seq`` the call is a
        NO-OP — the head never moves backwards.  This is the anti-rollback
        guard: a buggy or malicious lower-seq write must not roll the persisted
        head back.  The chain only ever advances, so legitimate writes always
        carry a strictly greater seq; an equal seq is treated as
        already-recorded.
        """
        ...


async def bootstrap_control_plane(config: ControlPlaneConfig) -> ControlPlane:
    if config.backend == "memory":
        from provide.uterm.control.plane.memory import MemoryControlPlane

        return MemoryControlPlane(config)
    if config.backend == "sqlite":
        from provide.uterm.control.plane.sqlite import SqliteControlPlane

        return SqliteControlPlane(config)
    raise ValueError(f"unsupported control-plane backend: {config.backend}")
