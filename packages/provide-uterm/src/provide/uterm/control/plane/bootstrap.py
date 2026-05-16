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


async def bootstrap_control_plane(config: ControlPlaneConfig) -> ControlPlane:
    if config.backend == "memory":
        from provide.uterm.control.plane.memory import MemoryControlPlane

        return MemoryControlPlane(config)
    if config.backend == "sqlite":
        from provide.uterm.control.plane.sqlite import SqliteControlPlane

        return SqliteControlPlane(config)
    raise ValueError(f"unsupported control-plane backend: {config.backend}")
