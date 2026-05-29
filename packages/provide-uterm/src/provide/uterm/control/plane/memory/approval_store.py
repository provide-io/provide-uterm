#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from provide.uterm.control.plane.approval.types import ApprovalRecord
    from provide.uterm.control.plane.memory.transaction import MemoryState, MemoryTransaction


class MemoryApprovalStore:
    def __init__(self, state: MemoryState, tx: MemoryTransaction) -> None:
        self._state = state
        self._tx = tx

    async def put_approval(self, record: ApprovalRecord) -> None:
        self._state.approvals[record.approval_id] = record

    async def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        return self._state.approvals.get(approval_id)

    async def list_pending(self) -> list[ApprovalRecord]:
        pending = [record for record in self._state.approvals.values() if record.state == "pending"]
        # Match the sqlite backend's ORDER BY created_at ASC, approval_id ASC
        # so FIFO consumers see the same order regardless of backend.
        return sorted(pending, key=lambda record: (record.created_at, record.approval_id))
