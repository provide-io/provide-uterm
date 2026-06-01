#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Per-agent status, events, and control API routes."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast

from fastapi import Depends, Path, Request
from fastapi.responses import JSONResponse
from provide.telemetry import get_logger
from pydantic import ValidationError

from provide.uterm.manager.routes.models import get_identity_store, get_managed_agent_plugin, require_manager, router

if TYPE_CHECKING:
    from provide.uterm.manager.core import AgentManager
    from provide.uterm.manager.models import AgentStatusBase

logger = get_logger(__name__)


# Operator-authority fields a worker self-report (POST /agent/{id}/register)
# must NEVER set. A low-privilege worker token is accepted on /register, so the
# attacker-controlled body is constrained against these to prevent privilege
# escalation (e.g. injecting a set_goal/set_directive operator command into an
# agent's queue, claiming hijack/pause state, or forging command history):
#   * pending_command_seq/_type/_payload — the operator command queue, set only
#     by the set-goal/set-directive operator routes and delivered to the agent
#     runtime by the status poll. A worker setting these is the priv-esc.
#   * manager_command_history — operator/manager-owned audit trail of commands.
#   * is_hijacked/hijacked_by/hijacked_at — hijack-lease state owned by the
#     manager; a self-report must not forge a hijack claim.
#   * paused — operator pause control (status returns agent.paused), not a
#     worker self-report field.
# pid/session_id/state/last_action/error_* are legitimately self-reported and
# are intentionally NOT in this set (see the status route's allowed fields).
_OPERATOR_FIELDS: frozenset[str] = frozenset(
    {
        "pending_command_seq",
        "pending_command_type",
        "pending_command_payload",
        "manager_command_history",
        "is_hijacked",
        "hijacked_by",
        "hijacked_at",
        "paused",
    }
)


# --- Command history helpers ---


def _command_history_rows(agent_status: AgentStatusBase) -> list[dict[str, Any]]:
    rows = getattr(agent_status, "manager_command_history", None)
    if not isinstance(rows, list):
        rows = []
        agent_status.manager_command_history = rows
    return rows


def _append_command_history(agent_status: AgentStatusBase, entry: dict[str, Any]) -> None:
    rows = _command_history_rows(agent_status)
    rows.append(dict(entry))
    if len(rows) > 25:
        del rows[:-25]


def _update_command_history(agent_status: AgentStatusBase, seq: int, **updates: Any) -> None:
    if seq <= 0:
        return
    for row in reversed(_command_history_rows(agent_status)):
        if int(row.get("seq") or 0) != seq:
            continue
        row.update(updates)
        return


def _queue_manager_command(agent_status: AgentStatusBase, command_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    replaced_seq = int(getattr(agent_status, "pending_command_seq", 0) or 0)
    if replaced_seq > 0 and getattr(agent_status, "pending_command_type", None):
        _update_command_history(
            agent_status,
            replaced_seq,
            status="replaced",
            replaced_by=replaced_seq + 1,
            updated_at=time.time(),
        )
    agent_status.pending_command_seq = replaced_seq + 1
    agent_status.pending_command_type = command_type
    agent_status.pending_command_payload = dict(payload)
    queued = {
        "seq": agent_status.pending_command_seq,
        "type": command_type,
        "payload": dict(payload),
        "replaces": replaced_seq if replaced_seq > 0 else None,
    }
    _append_command_history(
        agent_status,
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
    agent_status: AgentStatusBase, reason: str = "operator_cancelled"
) -> dict[str, Any] | None:
    pending_seq = int(getattr(agent_status, "pending_command_seq", 0) or 0)
    pending_type = str(getattr(agent_status, "pending_command_type", "") or "")
    if pending_seq <= 0 or not pending_type:
        return None
    cancelled = {
        "seq": pending_seq,
        "type": pending_type,
        "payload": dict(getattr(agent_status, "pending_command_payload", {}) or {}),
        "cancelled_reason": reason,
    }
    _update_command_history(
        agent_status, pending_seq, status="cancelled", cancelled_reason=reason, updated_at=time.time()
    )
    agent_status.pending_command_seq = 0
    agent_status.pending_command_type = None
    agent_status.pending_command_payload = {}
    return cancelled


def _build_action_response(
    agent_id: str,
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
                agent_id, action, source, applied=applied, queued=queued, result=result, state=state
            ),
        )
    return {
        "agent_id": agent_id,
        "action": action,
        "source": source,
        "applied": applied,
        "queued": queued,
        "result": result,
        "state": state,
    }


# --- Routes ---


@router.get("/agents")
async def list_agents(
    state: str | None = None,
    interactive_only: bool = False,
    manager: AgentManager = Depends(require_manager),  # noqa: B008
) -> Any:
    rows = []
    for agent in manager.agents.values():
        if state is not None and agent.state != state:
            continue
        config_value = str(agent.config or "")
        interactive = bool(agent.session_id) and config_value.startswith("mcp://")
        if interactive_only and not interactive:
            continue
        row = agent.model_dump()
        row["interactive"] = interactive
        rows.append(row)
    rows.sort(
        key=lambda item: (float(item.get("last_update_time") or 0), str(item.get("agent_id") or "")), reverse=True
    )
    return {"total": len(rows), "agents": rows}


@router.get("/agent/{agent_id}/status")
async def agent_status(agent_id: str, request: Request, manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    if agent_id not in manager.agents:
        return JSONResponse({"error": f"Agent {agent_id} not found"}, status_code=404)
    row = manager.agents[agent_id].model_dump()
    plugin = get_managed_agent_plugin(request)
    if plugin is not None:
        local_agent, local_session_id = plugin.resolve_local_agent(manager.agents[agent_id])
        runtime = plugin.describe_runtime(local_agent, local_session_id)
        if runtime is not None:
            row["local_runtime"] = runtime
    return row


@router.get("/agent/{agent_id}/details")
async def agent_details(agent_id: str, request: Request, manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    if agent_id not in manager.agents:
        return JSONResponse({"error": f"Agent {agent_id} not found"}, status_code=404)
    bs = manager.agents[agent_id]
    plugin = get_managed_agent_plugin(request)
    if plugin is not None:
        local_agent, local_session_id = plugin.resolve_local_agent(bs)
        return plugin.build_details(bs, local_agent, local_session_id)
    return bs.model_dump()


@router.get("/agent/{agent_id}/session-data")
async def agent_session_data(agent_id: str, request: Request) -> Any:
    store = get_identity_store(request)
    if store is None:
        return JSONResponse({"error": "Identity store not configured"}, status_code=503)
    record = store.load(agent_id)
    if record is None:
        return JSONResponse({"error": f"No persisted session data for {agent_id}"}, status_code=404)
    if hasattr(record, "model_dump"):
        return record.model_dump(mode="json")
    return dict(record) if isinstance(record, dict) else {"record": str(record)}


@router.post("/agent/{agent_id}/register")
async def register_agent(
    data: dict[str, Any],
    agent_id: str = Path(pattern=r"^[\w\-]+$"),
    manager: AgentManager = Depends(require_manager),  # noqa: B008
) -> Any:
    now = time.time()
    # Strict-reject (not silent-strip): a worker self-report has no legitimate
    # reason to send operator-authority fields, so a body containing any is
    # 422'd loudly. This makes a privilege-escalation attempt visible/auditable
    # rather than silently swallowed (V-H2).
    rejected = _OPERATOR_FIELDS.intersection(data)
    if rejected:
        return JSONResponse(
            {"error": f"register may not set operator-authority fields: {sorted(rejected)}"},
            status_code=422,
        )
    created = agent_id not in manager.agents
    if created and len(manager.agents) >= manager.max_agents:
        # Auto-create must honor max_agents so any token holder cannot grow the
        # agent registry without bound (PLAT-reg).
        return JSONResponse(
            {"error": f"Max agents ({manager.max_agents}) reached"},
            status_code=429,
        )
    # An EXISTING agent's base_payload carries its real operator-field values, so
    # omitting them from the body preserves them (a worker can't overwrite); a
    # NEW agent gets the model defaults.
    base_payload: dict[str, Any] = {"agent_id": agent_id} if created else manager.agents[agent_id].model_dump()
    try:
        merged = manager._agent_status_class.model_validate(
            {
                **base_payload,
                **data,
                "agent_id": agent_id,
                "session_id": data.get("session_id") or base_payload.get("session_id") or agent_id,
                "last_update_time": now,
            }
        )
    except ValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    manager.agents[agent_id] = merged
    return {"ok": True, "created": created}


@router.post("/agent/{agent_id}/set-goal")
async def set_goal(agent_id: str, goal: str, request: Request, manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    if agent_id not in manager.agents:
        return JSONResponse({"error": f"Agent {agent_id} not found"}, status_code=404)
    bs = manager.agents[agent_id]
    plugin = get_managed_agent_plugin(request)
    if plugin is not None:
        local_agent, _ = plugin.resolve_local_agent(bs)
        if local_agent is not None:
            local_result = await plugin.dispatch_command(local_agent, "set_goal", goal=goal)
            if "error" in local_result:
                return JSONResponse({"error": str(local_result["error"])}, status_code=400)
            return _build_action_response(
                agent_id,
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
        agent_id,
        "set_goal",
        "worker_queue",
        applied=False,
        queued=True,
        result={"goal": goal, "queued_command": queued},
        state=str(bs.state or "unknown"),
        plugin=plugin,
    )


@router.post("/agent/{agent_id}/set-directive")
async def set_directive(
    agent_id: str,
    data: dict[str, Any],
    request: Request,
    manager: AgentManager = Depends(require_manager),  # noqa: B008
) -> Any:
    if agent_id not in manager.agents:
        return JSONResponse({"error": f"Agent {agent_id} not found"}, status_code=404)
    bs = manager.agents[agent_id]
    plugin = get_managed_agent_plugin(request)
    directive_str = str(data.get("directive") or "")
    turns_val = int(data.get("turns") or 0)
    if plugin is not None:
        local_agent, _ = plugin.resolve_local_agent(bs)
        if local_agent is not None:
            local_result = await plugin.dispatch_command(
                local_agent, "set_directive", directive=directive_str, turns=turns_val
            )
            if "error" in local_result:
                return JSONResponse({"error": str(local_result["error"])}, status_code=400)
            return _build_action_response(
                agent_id,
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
        agent_id,
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


@router.post("/agent/{agent_id}/cancel-command")
async def cancel_command(agent_id: str, request: Request, manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    if agent_id not in manager.agents:
        return JSONResponse({"error": f"Agent {agent_id} not found"}, status_code=404)
    bs = manager.agents[agent_id]
    plugin = get_managed_agent_plugin(request)
    cancelled = _cancel_pending_manager_command(bs)
    if cancelled is None:
        return _build_action_response(
            agent_id,
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
        agent_id,
        "cancel_command",
        "manager",
        applied=True,
        queued=False,
        result={"cancelled": True, "cancelled_command": cancelled},
        state=str(bs.state or "unknown"),
        plugin=plugin,
    )


@router.delete("/agent/{agent_id}")
async def kill(agent_id: str, request: Request, manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    if agent_id not in manager.agents:
        return JSONResponse({"error": f"Agent {agent_id} not found"}, status_code=404)
    plugin = get_managed_agent_plugin(request)
    terminal_states = {"error", "stopped", "completed"}
    if manager.agents[agent_id].state in terminal_states:
        manager.agent_process_manager.release_agent_account(agent_id)
        del manager.agents[agent_id]
        await manager.broadcast_status()
        return _build_action_response(
            agent_id,
            "remove",
            "manager",
            applied=True,
            queued=False,
            result={"removed": agent_id, "desired_agents": manager.desired_agents},
            state="removed",
            plugin=plugin,
        )
    if agent_id not in manager.processes:
        manager.agent_process_manager.release_agent_account(agent_id)
        del manager.agents[agent_id]
        await manager.broadcast_status()
        return _build_action_response(
            agent_id,
            "remove",
            "manager",
            applied=True,
            queued=False,
            result={"removed": agent_id, "desired_agents": manager.desired_agents},
            state="removed",
            plugin=plugin,
        )
    await manager.kill_agent(agent_id)
    if manager.desired_agents > 0:
        manager.desired_agents = max(0, manager.desired_agents - 1)
    return _build_action_response(
        agent_id,
        "remove",
        "manager",
        applied=True,
        queued=False,
        result={"killed": agent_id, "desired_agents": manager.desired_agents},
        state="removed",
        plugin=plugin,
    )


@router.get("/agent/{agent_id}/events")
async def get_agent_events(agent_id: str, manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    if agent_id not in manager.agents:
        return JSONResponse({"error": f"Agent {agent_id} not found", "events": []}, status_code=404)
    agent = manager.agents[agent_id]
    events: list[dict[str, Any]] = []

    recent_actions = getattr(agent, "recent_actions", None) or []
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

    error_timestamp = getattr(agent, "error_timestamp", None)
    if error_timestamp:
        events.append(
            {
                "timestamp": error_timestamp,
                "type": "error",
                "error_type": getattr(agent, "error_type", None),
                "error_message": agent.error_message,
                "state": agent.state,
            }
        )

    if agent.last_update_time and not recent_actions:
        events.append(
            {
                "timestamp": agent.last_update_time,
                "type": "status_update",
                "state": agent.state,
            }
        )

    events.sort(key=lambda e: float(e["timestamp"] or 0), reverse=True)
    return {"agent_id": agent_id, "state": agent.state, "events": events[:50]}
