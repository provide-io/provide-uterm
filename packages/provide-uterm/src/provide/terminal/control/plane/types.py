from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, NewType

from provide.terminal.control.plane.capability import EngineCapabilities

ControlPlaneBackend = Literal["memory", "sqlite"]

ControlPlaneId = NewType("ControlPlaneId", str)
ControlPlaneScope = NewType("ControlPlaneScope", str)


@dataclass(frozen=True, slots=True)
class ControlPlaneRef:
    """Stable identifier for a control-plane backend."""

    control_plane_id: ControlPlaneId
    scope: ControlPlaneScope = ControlPlaneScope("default")


@dataclass(frozen=True, slots=True)
class ControlPlaneConfig:
    """Bootstrap configuration for control-plane backends."""

    backend: ControlPlaneBackend = "memory"
    database_url: str = ":memory:"
    capabilities: EngineCapabilities = field(default_factory=EngineCapabilities)
