#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Per-bot status update (POST /bot/{bot_id}/status) API route.

Applies base fields generically, then delegates to a
``StatusUpdatePlugin`` for game-specific field merging.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from fastapi import Body, Depends, Path, Request
from pydantic import BaseModel, ConfigDict
from provide.telemetry import get_logger

from provide.terminal.manager.routes.bot_ops import _update_command_history
from provide.terminal.manager.routes.models import require_manager, router

if TYPE_CHECKING:
    from provide.terminal.manager.core import SwarmManager

logger = get_logger(__name__)


class StatusUpdatePayload(BaseModel):
    """Validated base status update payload from worker bots.

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


@router.post("/bot/{bot_id}/status")
async def update_status(
    request: Request,  # noqa: ARG001
    bot_id: str = Path(pattern=r"^[\w\-]+$"),
    update: StatusUpdatePayload = Body(...),  # noqa: B008
    manager: SwarmManager = Depends(require_manager),  # noqa: B008
) -> Any:
    payload = update.model_dump(exclude_unset=True)
    if bot_id not in manager.bots:
        manager.bots[bot_id] = manager._bot_status_class(
            bot_id=bot_id,
            pid=0,
            state="running",
            started_at=time.time(),
        )
    bot = manager.bots[bot_id]

    # Manager command acknowledgement
    ack_seq = int(payload.get("last_manager_command_seq") or 0)
    if ack_seq > 0 and ack_seq == int(bot.pending_command_seq or 0):
        _update_command_history(bot, ack_seq, status="acknowledged", updated_at=time.time())
        bot.pending_command_seq = 0
        bot.pending_command_type = None
        bot.pending_command_payload = {}

    # Stale-report rejection
    try:
        incoming_reported_at = float(payload.get("reported_at") or 0.0)
    except Exception:  # pragma: no cover — Pydantic validates float before reaching here
        incoming_reported_at = 0.0
    status_reported_at = getattr(bot, "status_reported_at", 0.0) or 0.0
    if incoming_reported_at > 0 and status_reported_at > 0 and incoming_reported_at < status_reported_at:
        return {"ok": True, "ignored": "stale_report"}
    if incoming_reported_at > 0:
        bot.status_reported_at = incoming_reported_at

    # Apply base fields
    if payload.get("pid"):
        bot.pid = int(payload["pid"])
    if "state" in payload:
        bot.state = payload["state"]
    if "started_at" in payload:
        bot.started_at = float(payload["started_at"]) if payload["started_at"] is not None else None
    if "stopped_at" in payload:
        bot.stopped_at = float(payload["stopped_at"]) if payload["stopped_at"] is not None else None
    if "last_action" in payload:
        bot.last_action = payload["last_action"]
    if "last_action_time" in payload:
        bot.last_action_time = payload["last_action_time"]
    if "error_message" in payload:
        bot.error_message = payload["error_message"]
    if "error_type" in payload:
        bot.error_type = payload["error_type"]
    if "error_timestamp" in payload:
        bot.error_timestamp = payload["error_timestamp"]
    if "exit_reason" in payload:
        bot.exit_reason = payload["exit_reason"]
    if "recent_actions" in payload:
        bot.recent_actions = payload["recent_actions"]

    # Delegate game-specific field merging to plugin
    status_update_plugin = manager._status_update_plugin
    if status_update_plugin is not None:
        status_update_plugin.apply_update(bot, payload, manager)

    bot.last_update_time = time.time()
    await manager.broadcast_status()

    response: dict[str, Any] = {"ok": True, "paused": bot.paused or manager.swarm_paused}
    if (
        bot.pending_command_type
        and int(bot.pending_command_seq or 0) > 0
        and int(bot.pending_command_seq or 0) != ack_seq
    ):
        response["manager_command"] = {
            "seq": int(bot.pending_command_seq or 0),
            "type": str(bot.pending_command_type or ""),
            "payload": dict(bot.pending_command_payload or {}),
        }
    return response
