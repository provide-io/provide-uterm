#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
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
from provide.telemetry import get_logger
from pydantic import BaseModel, ConfigDict

from provide.terminal.manager.routes.agent_ops import _update_command_history
from provide.terminal.manager.routes.models import require_manager, router

if TYPE_CHECKING:
    from provide.terminal.manager.core import AgentManager

logger = get_logger(__name__)

_FLOAT_TIMESTAMP_FIELDS: frozenset[str] = frozenset({"started_at", "stopped_at"})
_DIRECT_ASSIGN_FIELDS: frozenset[str] = frozenset(
    {
        "state",
        "last_action",
        "last_action_time",
        "error_message",
        "error_type",
        "error_timestamp",
        "exit_reason",
        "recent_actions",
    }
)


def _acknowledge_command(agent: Any, ack_seq: int) -> None:
    """Clear pending command when the agent acknowledges it."""
    if ack_seq > 0 and ack_seq == int(agent.pending_command_seq or 0):
        _update_command_history(agent, ack_seq, status="acknowledged", updated_at=time.time())
        agent.pending_command_seq = 0
        agent.pending_command_type = None
        agent.pending_command_payload = {}


def _check_stale_report(agent: Any, payload: dict[str, Any]) -> bool:
    """Return True if this status report is older than the last accepted one."""
    try:
        incoming = float(payload.get("reported_at") or 0.0)
    except Exception:  # pragma: no cover — Pydantic validates float before reaching here
        incoming = 0.0
    stored = getattr(agent, "status_reported_at", 0.0) or 0.0
    if incoming > 0 and stored > 0 and incoming < stored:
        return True
    if incoming > 0:
        agent.status_reported_at = incoming
    return False


def _apply_base_fields(agent: Any, payload: dict[str, Any]) -> None:
    """Apply standard base status fields from *payload* onto *agent*."""
    if payload.get("pid"):
        agent.pid = int(payload["pid"])
    for field in _FLOAT_TIMESTAMP_FIELDS:
        if field in payload:
            val = payload[field]
            setattr(agent, field, float(val) if val is not None else None)
    for field in _DIRECT_ASSIGN_FIELDS:
        if field in payload:
            setattr(agent, field, payload[field])


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


def _build_pending_command_response(agent: Any, ack_seq: int) -> dict[str, Any] | None:
    """Build the manager_command dict if a pending command exists; otherwise return None."""
    seq = int(agent.pending_command_seq or 0)
    if not agent.pending_command_type or seq <= 0 or seq == ack_seq:
        return None
    return {
        "seq": seq,
        "type": str(agent.pending_command_type or ""),
        "payload": dict(agent.pending_command_payload or {}),
    }


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

    ack_seq = int(payload.get("last_manager_command_seq") or 0)
    _acknowledge_command(agent, ack_seq)

    if _check_stale_report(agent, payload):
        return {"ok": True, "ignored": "stale_report"}

    _apply_base_fields(agent, payload)

    if manager._status_update_plugin is not None:
        manager._status_update_plugin.apply_update(agent, payload, manager)

    agent.last_update_time = time.time()
    await manager.broadcast_status()

    response: dict[str, Any] = {"ok": True, "paused": agent.paused or manager.swarm_paused}
    cmd = _build_pending_command_response(agent, ack_seq)
    if cmd is not None:
        response["manager_command"] = cmd
    return response
