#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Per-agent status update (POST /agent/{agent_id}/status) API route.

Applies base fields generically, then delegates to a
``StatusUpdatePlugin`` for game-specific field merging.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from fastapi import Body, Depends, Path, Request
from pydantic import BaseModel, ConfigDict
from provide.telemetry import get_logger

from provide.terminal.manager.routes.agent_ops import _update_command_history
from provide.terminal.manager.routes.models import require_manager, router

if TYPE_CHECKING:
    from provide.terminal.manager.core import AgentManager

logger = get_logger(__name__)


class StatusUpdatePayload(BaseModel):
    """Validated base status update payload from worker agents.

    Game-specific plugins can extend this or accept extra fields
    via ``extra="allow"`` on their own payload model.
    """

    model_config = ConfigDict(extra="allow")

    reported_at: float | None = None
    last_manager_command_seq: int | None = None
    pid: int | None = None
    state: str | None = None
    started_at: float | None = None
    stopped_at: float | None = None
    last_action: str | None = None
    last_action_time: float | None = None
    error_message: str | None = None
    error_type: str | None = None
    error_timestamp: float | None = None
    exit_reason: str | None = None
    recent_actions: list[dict[str, Any]] | None = None


@router.post("/agent/{agent_id}/status")
async def update_status(
    request: Request,  # noqa: ARG001
    agent_id: str = Path(pattern=r"^[\w\-]+$"),
    update: StatusUpdatePayload = Body(...),  # noqa: B008
    manager: AgentManager = Depends(require_manager),  # noqa: B008
) -> Any:
    payload = update.model_dump(exclude_unset=True)
    if agent_id not in manager.agents:
        manager.agents[agent_id] = manager._agent_status_class(
            agent_id=agent_id,
            pid=0,
            state="running",
            started_at=time.time(),
        )
    agent = manager.agents[agent_id]

    # Manager command acknowledgement
    ack_seq = int(payload.get("last_manager_command_seq") or 0)
    if ack_seq > 0 and ack_seq == int(agent.pending_command_seq or 0):
        _update_command_history(agent, ack_seq, status="acknowledged", updated_at=time.time())
        agent.pending_command_seq = 0
        agent.pending_command_type = None
        agent.pending_command_payload = {}

    # Stale-report rejection
    try:
        incoming_reported_at = float(payload.get("reported_at") or 0.0)
    except Exception:  # pragma: no cover — Pydantic validates float before reaching here
        incoming_reported_at = 0.0
    status_reported_at = getattr(agent, "status_reported_at", 0.0) or 0.0
    if incoming_reported_at > 0 and status_reported_at > 0 and incoming_reported_at < status_reported_at:
        return {"ok": True, "ignored": "stale_report"}
    if incoming_reported_at > 0:
        agent.status_reported_at = incoming_reported_at

    # Apply base fields
    if payload.get("pid"):
        agent.pid = int(payload["pid"])
    if "state" in payload:
        agent.state = payload["state"]
    if "started_at" in payload:
        agent.started_at = float(payload["started_at"]) if payload["started_at"] is not None else None
    if "stopped_at" in payload:
        agent.stopped_at = float(payload["stopped_at"]) if payload["stopped_at"] is not None else None
    if "last_action" in payload:
        agent.last_action = payload["last_action"]
    if "last_action_time" in payload:
        agent.last_action_time = payload["last_action_time"]
    if "error_message" in payload:
        agent.error_message = payload["error_message"]
    if "error_type" in payload:
        agent.error_type = payload["error_type"]
    if "error_timestamp" in payload:
        agent.error_timestamp = payload["error_timestamp"]
    if "exit_reason" in payload:
        agent.exit_reason = payload["exit_reason"]
    if "recent_actions" in payload:
        agent.recent_actions = payload["recent_actions"]

    # Delegate game-specific field merging to plugin
    status_update_plugin = manager._status_update_plugin
    if status_update_plugin is not None:
        status_update_plugin.apply_update(agent, payload, manager)

    agent.last_update_time = time.time()
    await manager.broadcast_status()

    response: dict[str, Any] = {"ok": True, "paused": agent.paused or manager.swarm_paused}
    if (
        agent.pending_command_type
        and int(agent.pending_command_seq or 0) > 0
        and int(agent.pending_command_seq or 0) != ack_seq
    ):
        response["manager_command"] = {
            "seq": int(agent.pending_command_seq or 0),
            "type": str(agent.pending_command_type or ""),
            "payload": dict(agent.pending_command_payload or {}),
        }
    return response
