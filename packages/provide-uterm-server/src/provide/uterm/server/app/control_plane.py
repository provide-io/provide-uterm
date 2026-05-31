#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Control-plane backend selection for the hosted terminal server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from provide.uterm.control.plane import ControlPlane as SharedControlPlane
from provide.uterm.control.plane import ControlPlaneConfig as SharedControlPlaneConfig
from provide.uterm.control.plane.memory import MemoryControlPlane
from provide.uterm.control.plane.sqlite import SqliteControlPlane

if TYPE_CHECKING:
    from provide.uterm.server.models import ServerConfig


@dataclass(frozen=True, slots=True)
class DurabilityCapabilities:
    """Durability posture for the FastAPI reference server."""

    control_plane_backend: str
    durable_state: tuple[str, ...]
    process_local_state: tuple[str, ...]
    ha_safe: bool
    notes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "control_plane_backend": self.control_plane_backend,
            "durable_state": list(self.durable_state),
            "process_local_state": list(self.process_local_state),
            "ha_safe": self.ha_safe,
            "notes": list(self.notes),
        }


def _build_control_plane(config: ServerConfig) -> SharedControlPlane:
    shared_config = SharedControlPlaneConfig(
        backend=config.control_plane.backend,
        database_url=config.control_plane.database_url or ":memory:",
    )
    if shared_config.backend == "sqlite":
        return SqliteControlPlane(shared_config)
    return MemoryControlPlane(shared_config)


def _build_durability_capabilities(config: ServerConfig) -> DurabilityCapabilities:
    backend = str(config.control_plane.backend)
    durable_state: tuple[str, ...] = ()
    if backend == "sqlite":
        # Only the resume-token store is wired into the reference server (via
        # ControlPlaneResumeStore -> token_store). Session records are NOT
        # written to the control plane (no ControlPlaneSessionStore), approvals
        # use the InMemoryApprovalStore, and leases live in WorkerTermState — so
        # the resume-token store is the sole durable surface.
        durable_state = ("resume_tokens",)
    process_local_state = (
        "tunnel_tokens",
        "webhook_registrations",
        "fanout_groups",
        "approvals",
        "leases",
        "live_session_arbitration",
        "session_registry_runtime_state",
    )
    notes = (
        "SQLite mode persists only the resume-token store wired into the factory.",
        "Session records, approvals, and hijack leases are in-memory and are lost on restart.",
        "Tunnel tokens, webhook registrations, and fan-out groups remain process-local.",
        "Run one active FastAPI control-plane instance, or use the durable backend for HA deployments.",
    )
    return DurabilityCapabilities(
        control_plane_backend=backend,
        durable_state=durable_state,
        process_local_state=process_local_state,
        ha_safe=False,
        notes=notes,
    )
