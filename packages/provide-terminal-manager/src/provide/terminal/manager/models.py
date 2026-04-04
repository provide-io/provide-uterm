#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Swarm manager data models and static file handler."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.responses import Response  # noqa: TC002

if TYPE_CHECKING:
    from starlette.types import Scope

__all__ = [
    "AgentStatusBase",
    "DashboardStaticFiles",
    "SpawnBatchRequest",
    "SwarmStatus",
]

NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


class DashboardStaticFiles(StaticFiles):
    """Static files mount with no-store headers for dashboard frontend assets."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if path in {"dashboard.js"}:
            for key, value in NO_STORE_HEADERS.items():
                response.headers[key] = value
        return response


class AgentStatusBase(BaseModel):
    """Game-agnostic status fields shared by all agent types.

    Each game extends this with game-specific status fields.
    The ``state`` field tracks the agent's life-cycle: queued, running,
    paused, completed, or error.
    """

    agent_id: str
    session_id: str | None = None
    state: str = "unknown"
    pid: int | None = None
    config: str | None = None
    started_at: float | None = None
    stopped_at: float | None = None
    completed_at: float | None = None
    last_update_time: float = 0.0
    error_message: str | None = None
    error_type: str | None = None
    error_timestamp: float | None = None
    exit_reason: str | None = None
    last_action: str | None = None
    last_action_time: float | None = None
    status_reported_at: float | None = None
    recent_actions: list[dict[str, Any]] = Field(default_factory=list)
    is_hijacked: bool = False
    hijacked_by: str | None = None
    hijacked_at: float | None = None
    paused: bool = False
    respawned_from: str | None = None
    pending_command_seq: int = 0
    pending_command_type: str | None = None
    pending_command_payload: dict[str, Any] = Field(default_factory=dict)
    manager_command_history: list[dict[str, Any]] = Field(default_factory=list)


class SwarmStatus(BaseModel):
    """Overall swarm status.

    Uses ``extra="allow"`` so game plugins can inject aggregate fields
    (e.g. total_credits) without modifying this base model.
    """

    model_config = {"extra": "allow"}

    total_agents: int
    running: int
    completed: int
    errors: int
    stopped: int
    uptime_seconds: float
    timeseries_file: str | None = None
    timeseries_interval_seconds: int = 0
    timeseries_samples: int = 0
    swarm_paused: bool = False
    bust_respawn: bool = False
    desired_agents: int = 0
    agents: list[Any]


class SpawnBatchRequest(BaseModel):
    """Request body for batch spawning agents."""

    config_paths: list[str] = Field(min_length=1)
    group_size: int = Field(default=1, gt=0)
    group_delay: float = Field(default=12.0, ge=0.0)
    name_style: str = "random"
    name_base: str = ""
