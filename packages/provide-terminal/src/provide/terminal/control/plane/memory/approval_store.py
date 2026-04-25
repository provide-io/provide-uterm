from __future__ import annotations

from provide.terminal.control.plane.approval.types import ApprovalRecord
from provide.terminal.control.plane.memory.transaction import MemoryState, MemoryTransaction


class MemoryApprovalStore:
    def __init__(self, state: MemoryState, tx: MemoryTransaction) -> None:
        self._state = state
        self._tx = tx

    async def put_approval(self, record: ApprovalRecord) -> None:
        self._state.approvals[record.approval_id] = record

    async def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        return self._state.approvals.get(approval_id)

    async def list_pending(self) -> list[ApprovalRecord]:
        return [record for record in self._state.approvals.values() if record.state == "pending"]
