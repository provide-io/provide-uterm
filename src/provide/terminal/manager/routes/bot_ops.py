#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Per-bot status, events, and control API routes."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from provide.telemetry import get_logger

from provide.terminal.manager.routes.models import get_identity_store, get_managed_bot_plugin, require_manager, router

if TYPE_CHECKING:
    from provide.terminal.manager.core import SwarmManager
    from provide.terminal.manager.models import BotStatusBase

logger = get_logger(__name__)


# --- Command history helpers ---


def _command_history_rows(bot_status: BotStatusBase) -> list[dict[str, Any]]:
    rows = getattr(bot_status, "manager_command_history", None)
    if not isinstance(rows, list):
        rows = []
        bot_status.manager_command_history = rows
    return rows


def _append_command_history(bot_status: BotStatusBase, entry: dict[str, Any]) -> None:
    rows = _command_history_rows(bot_status)
    rows.append(dict(entry))
    if len(rows) > 25:
        del rows[:-25]


def _update_command_history(bot_status: BotStatusBase, seq: int, **updates: Any) -> None:
    if seq <= 0:
        return
    for row in reversed(_command_history_rows(bot_status)):
        if int(row.get("seq") or 0) != seq:
            continue
        row.update(updates)
        return


def _queue_manager_command(bot_status: BotStatusBase, command_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    replaced_seq = int(getattr(bot_status, "pending_command_seq", 0) or 0)
    if replaced_seq > 0 and getattr(bot_status, "pending_command_type", None):
        _update_command_history(
            bot_status,
            replaced_seq,
            status="replaced",
            replaced_by=replaced_seq + 1,
            updated_at=time.time(),
        )
    bot_status.pending_command_seq = replaced_seq + 1
    bot_status.pending_command_type = command_type
    bot_status.pending_command_payload = dict(payload)
    queued = {
        "seq": bot_status.pending_command_seq,
        "type": command_type,
        "payload": dict(payload),
        "replaces": replaced_seq if replaced_seq > 0 else None,
    }
    _append_command_history(
        bot_status,
        {
            "seq": queued["seq"],
            "type": command_type,
            "payload": dict(payload),
            "status": "queued",
            "queued_at": time.time(),
            "updated_at": time.time(),
            "replaces": queued["replaces"],
            "replaced_by": None,
            "cancelled_reason": None,
        },
    )
    return queued


def _cancel_pending_manager_command(
    bot_status: BotStatusBase, reason: str = "operator_cancelled"
) -> dict[str, Any] | None:
    pending_seq = int(getattr(bot_status, "pending_command_seq", 0) or 0)
    pending_type = str(getattr(bot_status, "pending_command_type", "") or "")
    if pending_seq <= 0 or not pending_type:
        return None
    cancelled = {
        "seq": pending_seq,
        "type": pending_type,
        "payload": dict(getattr(bot_status, "pending_command_payload", {}) or {}),
        "cancelled_reason": reason,
    }
    _update_command_history(
        bot_status, pending_seq, status="cancelled", cancelled_reason=reason, updated_at=time.time()
    )
    bot_status.pending_command_seq = 0
    bot_status.pending_command_type = None
    bot_status.pending_command_payload = {}
    return cancelled


def _build_action_response(
    bot_id: str,
    action: str,
    source: str,
    *,
    applied: bool,
    queued: bool,
    result: dict[str, Any],
    state: str,
    plugin: Any | None = None,
) -> dict[str, Any]:
    if plugin is not None:
        return cast(
            "dict[str, Any]",
            plugin.build_action_response(
                bot_id, action, source, applied=applied, queued=queued, result=result, state=state
            ),
        )
    return {
        "bot_id": bot_id,
        "action": action,
        "source": source,
        "applied": applied,
        "queued": queued,
        "result": result,
        "state": state,
    }


# --- Routes ---


@router.get("/bots")
async def list_bots(
    state: str | None = None,
    interactive_only: bool = False,
    manager: SwarmManager = Depends(require_manager),  # noqa: B008
) -> Any:
    rows = []
    for bot in manager.bots.values():
        if state is not None and bot.state != state:
            continue
        config_value = str(bot.config or "")
        interactive = bool(bot.session_id) and config_value.startswith("mcp://")
        if interactive_only and not interactive:
            continue
        row = bot.model_dump()
        row["interactive"] = interactive
        rows.append(row)
    rows.sort(key=lambda item: (float(item.get("last_update_time") or 0), str(item.get("bot_id") or "")), reverse=True)
    return {"total": len(rows), "bots": rows}


@router.get("/bot/{bot_id}/status")
async def bot_status(bot_id: str, request: Request, manager: SwarmManager = Depends(require_manager)) -> Any:  # noqa: B008
    if bot_id not in manager.bots:
        return JSONResponse({"error": f"Bot {bot_id} not found"}, status_code=404)
    row = manager.bots[bot_id].model_dump()
    plugin = get_managed_bot_plugin(request)
    if plugin is not None:
        local_bot, local_session_id = plugin.resolve_local_bot(manager.bots[bot_id])
        runtime = plugin.describe_runtime(local_bot, local_session_id)
        if runtime is not None:
            row["local_runtime"] = runtime
    return row


@router.get("/bot/{bot_id}/details")
async def bot_details(bot_id: str, request: Request, manager: SwarmManager = Depends(require_manager)) -> Any:  # noqa: B008
    if bot_id not in manager.bots:
        return JSONResponse({"error": f"Bot {bot_id} not found"}, status_code=404)
    bs = manager.bots[bot_id]
    plugin = get_managed_bot_plugin(request)
    if plugin is not None:
        local_bot, local_session_id = plugin.resolve_local_bot(bs)
        return plugin.build_details(bs, local_bot, local_session_id)
    return bs.model_dump()


@router.get("/bot/{bot_id}/session-data")
async def bot_session_data(bot_id: str, request: Request) -> Any:
    store = get_identity_store(request)
    if store is None:
        return JSONResponse({"error": "Identity store not configured"}, status_code=503)
    record = store.load(bot_id)
    if record is None:
        return JSONResponse({"error": f"No persisted session data for {bot_id}"}, status_code=404)
    if hasattr(record, "model_dump"):
        return record.model_dump(mode="json")
    return dict(record) if isinstance(record, dict) else {"record": str(record)}


@router.post("/bot/{bot_id}/register")
async def register_bot(bot_id: str, data: dict[str, Any], manager: SwarmManager = Depends(require_manager)) -> Any:  # noqa: B008
    now = time.time()
    created = bot_id not in manager.bots
    base_payload: dict[str, Any] = {"bot_id": bot_id} if created else manager.bots[bot_id].model_dump()
    try:
        merged = manager._bot_status_class.model_validate(
            {
                **base_payload,
                **data,
                "bot_id": bot_id,
                "session_id": data.get("session_id") or base_payload.get("session_id") or bot_id,
                "last_update_time": now,
            }
        )
    except ValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    manager.bots[bot_id] = merged
    return {"ok": True, "created": created}


@router.post("/bot/{bot_id}/set-goal")
async def set_goal(bot_id: str, goal: str, request: Request, manager: SwarmManager = Depends(require_manager)) -> Any:  # noqa: B008
    if bot_id not in manager.bots:
        return JSONResponse({"error": f"Bot {bot_id} not found"}, status_code=404)
    bs = manager.bots[bot_id]
    plugin = get_managed_bot_plugin(request)
    if plugin is not None:
        local_bot, _ = plugin.resolve_local_bot(bs)
        if local_bot is not None:
            local_result = await plugin.dispatch_command(local_bot, "set_goal", goal=goal)
            if "error" in local_result:
                return JSONResponse({"error": str(local_result["error"])}, status_code=400)
            return _build_action_response(
                bot_id,
                "set_goal",
                "local_runtime",
                applied=True,
                queued=False,
                result={"goal": goal, **local_result},
                state=str(bs.state or "unknown"),
                plugin=plugin,
            )
    queued = _queue_manager_command(bs, "set_goal", {"goal": goal})
    return _build_action_response(
        bot_id,
        "set_goal",
        "worker_queue",
        applied=False,
        queued=True,
        result={"goal": goal, "queued_command": queued},
        state=str(bs.state or "unknown"),
        plugin=plugin,
    )


@router.post("/bot/{bot_id}/set-directive")
async def set_directive(
    bot_id: str,
    data: dict[str, Any],
    request: Request,
    manager: SwarmManager = Depends(require_manager),  # noqa: B008
) -> Any:
    if bot_id not in manager.bots:
        return JSONResponse({"error": f"Bot {bot_id} not found"}, status_code=404)
    bs = manager.bots[bot_id]
    plugin = get_managed_bot_plugin(request)
    directive_str = str(data.get("directive") or "")
    turns_val = int(data.get("turns") or 0)
    if plugin is not None:
        local_bot, _ = plugin.resolve_local_bot(bs)
        if local_bot is not None:
            local_result = await plugin.dispatch_command(
                local_bot, "set_directive", directive=directive_str, turns=turns_val
            )
            if "error" in local_result:
                return JSONResponse({"error": str(local_result["error"])}, status_code=400)
            return _build_action_response(
                bot_id,
                "set_directive",
                "local_runtime",
                applied=True,
                queued=False,
                result={"directive": local_result.get("directive"), "turns": local_result.get("turns"), **local_result},
                state=str(bs.state or "unknown"),
                plugin=plugin,
            )
    queued = _queue_manager_command(bs, "set_directive", {"directive": directive_str, "turns": turns_val})
    return _build_action_response(
        bot_id,
        "set_directive",
        "worker_queue",
        applied=False,
        queued=True,
        result={
            "directive": queued["payload"].get("directive"),
            "turns": queued["payload"].get("turns"),
            "queued_command": queued,
        },
        state=str(bs.state or "unknown"),
        plugin=plugin,
    )


@router.post("/bot/{bot_id}/cancel-command")
async def cancel_command(bot_id: str, request: Request, manager: SwarmManager = Depends(require_manager)) -> Any:  # noqa: B008
    if bot_id not in manager.bots:
        return JSONResponse({"error": f"Bot {bot_id} not found"}, status_code=404)
    bs = manager.bots[bot_id]
    plugin = get_managed_bot_plugin(request)
    cancelled = _cancel_pending_manager_command(bs)
    if cancelled is None:
        return _build_action_response(
            bot_id,
            "cancel_command",
            "manager",
            applied=False,
            queued=False,
            result={"cancelled": False, "reason": "no_pending_command"},
            state=str(bs.state or "unknown"),
            plugin=plugin,
        )
    await manager.broadcast_status()
    return _build_action_response(
        bot_id,
        "cancel_command",
        "manager",
        applied=True,
        queued=False,
        result={"cancelled": True, "cancelled_command": cancelled},
        state=str(bs.state or "unknown"),
        plugin=plugin,
    )


@router.delete("/bot/{bot_id}")
async def kill(bot_id: str, request: Request, manager: SwarmManager = Depends(require_manager)) -> Any:  # noqa: B008
    if bot_id not in manager.bots:
        return JSONResponse({"error": f"Bot {bot_id} not found"}, status_code=404)
    plugin = get_managed_bot_plugin(request)
    terminal_states = {"error", "stopped", "completed"}
    if manager.bots[bot_id].state in terminal_states:
        manager.bot_process_manager.release_bot_account(bot_id)
        del manager.bots[bot_id]
        await manager.broadcast_status()
        return _build_action_response(
            bot_id,
            "remove",
            "manager",
            applied=True,
            queued=False,
            result={"removed": bot_id, "desired_bots": manager.desired_bots},
            state="removed",
            plugin=plugin,
        )
    if bot_id not in manager.processes:
        manager.bot_process_manager.release_bot_account(bot_id)
        del manager.bots[bot_id]
        await manager.broadcast_status()
        return _build_action_response(
            bot_id,
            "remove",
            "manager",
            applied=True,
            queued=False,
            result={"removed": bot_id, "desired_bots": manager.desired_bots},
            state="removed",
            plugin=plugin,
        )
    await manager.kill_bot(bot_id)
    if manager.desired_bots > 0:
        manager.desired_bots = max(0, manager.desired_bots - 1)
    return _build_action_response(
        bot_id,
        "remove",
        "manager",
        applied=True,
        queued=False,
        result={"killed": bot_id, "desired_bots": manager.desired_bots},
        state="removed",
        plugin=plugin,
    )


@router.get("/bot/{bot_id}/events")
async def get_bot_events(bot_id: str, manager: SwarmManager = Depends(require_manager)) -> Any:  # noqa: B008
    if bot_id not in manager.bots:
        return JSONResponse({"error": f"Bot {bot_id} not found", "events": []}, status_code=404)
    bot = manager.bots[bot_id]
    events: list[dict[str, Any]] = []

    recent_actions = getattr(bot, "recent_actions", None) or []
    for action in recent_actions:
        action_time = float(action.get("time", 0) or 0)
        events.append(
            {
                "timestamp": action_time,
                "type": "action",
                "action": action.get("action", "UNKNOWN"),
                "sector": action.get("sector"),
                "result": action.get("result"),
                "details": action.get("details"),
            }
        )

    error_timestamp = getattr(bot, "error_timestamp", None)
    if error_timestamp:
        events.append(
            {
                "timestamp": error_timestamp,
                "type": "error",
                "error_type": getattr(bot, "error_type", None),
                "error_message": bot.error_message,
                "state": bot.state,
            }
        )

    if bot.last_update_time and not recent_actions:
        events.append(
            {
                "timestamp": bot.last_update_time,
                "type": "status_update",
                "state": bot.state,
            }
        )

    events.sort(key=lambda e: float(e["timestamp"] or 0), reverse=True)
    return {"bot_id": bot_id, "state": bot.state, "events": events[:50]}
