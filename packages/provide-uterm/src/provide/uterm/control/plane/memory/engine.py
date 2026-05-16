from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from provide.uterm.control.plane.memory.approval_store import MemoryApprovalStore
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

    def __post_init__(self) -> None:
        self.capabilities = self.config.capabilities
        self._state = MemoryState()

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def migrate(self) -> None:
        return None

    async def begin(self) -> MemoryTransaction:
        return MemoryTransaction(self._state)

    def session_store(self, tx: MemoryTransaction) -> MemorySessionStore:
        return MemorySessionStore(self._state, tx)

    def token_store(self, tx: MemoryTransaction) -> MemoryTokenStore:
        return MemoryTokenStore(self._state, tx)

    def approval_store(self, tx: MemoryTransaction) -> MemoryApprovalStore:
        return MemoryApprovalStore(self._state, tx)

    def lease_store(self, tx: MemoryTransaction) -> MemoryLeaseStore:
        return MemoryLeaseStore(self._state, tx)
