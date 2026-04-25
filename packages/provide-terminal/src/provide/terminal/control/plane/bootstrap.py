from __future__ import annotations

from typing import Protocol

from provide.terminal.control.plane.capability import EngineCapabilities
from provide.terminal.control.plane.transaction import Transaction
from provide.terminal.control.plane.types import ControlPlaneConfig


class ControlPlane(Protocol):
    capabilities: EngineCapabilities

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def migrate(self) -> None: ...

    async def begin(self) -> Transaction: ...


async def bootstrap_control_plane(config: ControlPlaneConfig) -> ControlPlane:
    if config.backend == "memory":
        from provide.terminal.control.plane.memory import MemoryControlPlane

        return MemoryControlPlane(config)
    if config.backend == "sqlite":
        from provide.terminal.control.plane.sqlite import SqliteControlPlane

        return SqliteControlPlane(config)
    raise ValueError(f"unsupported control-plane backend: {config.backend}")
