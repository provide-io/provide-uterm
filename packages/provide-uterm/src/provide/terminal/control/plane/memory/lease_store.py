from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from provide.terminal.control.plane.lease.types import LeaseRecord
    from provide.terminal.control.plane.memory.transaction import MemoryState, MemoryTransaction


class MemoryLeaseStore:
    def __init__(self, state: MemoryState, tx: MemoryTransaction) -> None:
        self._state = state
        self._tx = tx

    async def put_lease(self, record: LeaseRecord) -> None:
        self._state.leases[record.session_id] = record

    async def get_lease(self, session_id: str) -> LeaseRecord | None:
        return self._state.leases.get(session_id)

    async def clear_lease(self, session_id: str) -> None:
        self._state.leases.pop(session_id, None)
