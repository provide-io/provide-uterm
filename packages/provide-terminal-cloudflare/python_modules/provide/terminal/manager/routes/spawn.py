#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Swarm spawn, kill, and fleet control API routes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from provide.telemetry import get_logger

from provide.terminal.manager.models import SpawnBatchRequest  # noqa: TC001
from provide.terminal.manager.routes.agent_ops import _queue_manager_command
from provide.terminal.manager.routes.models import get_managed_agent_plugin, require_manager, router

if TYPE_CHECKING:
    from provide.terminal.manager.core import AgentManager

logger = get_logger(__name__)


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
    """Build a standardised action response, delegating to plugin if available."""
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


@router.get("/health")
async def health_check() -> Any:
    return {"status": "ok"}


def _validate_config_path(config_path: str, *, config_dir_env: str = "") -> Path:
    """Validate config_path is a safe YAML file within the allowed directory."""
    resolved = Path(config_path).resolve()
    if resolved.suffix.lower() not in (".yaml", ".yml"):
        raise ValueError(f"config_path must be a .yaml or .yml file: {config_path}")
    env = config_dir_env or os.environ.get("UTERM_CONFIG_DIR", "").strip()
    if env:
        config_base = Path(env).resolve()
        if not resolved.is_relative_to(config_base):
            raise ValueError(f"config_path is outside config dir ({config_base}): {config_path}")
    return resolved


@router.post("/swarm/spawn")
async def spawn(config_path: str, agent_id: str = "", manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    try:
        _validate_config_path(config_path)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        if not agent_id:
            agent_id = manager.agent_process_manager.allocate_agent_id()
        else:
            manager.agent_process_manager.note_agent_id(agent_id)
        agent_id = await manager.spawn_agent(config_path, agent_id)
        return {"agent_id": agent_id, "pid": manager.agents[agent_id].pid}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/swarm/spawn-batch")
async def spawn_batch(request: SpawnBatchRequest, manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    total = len(request.config_paths)
    groups = (total + request.group_size - 1) // request.group_size

    await manager.start_spawn_swarm(
        request.config_paths,
        group_size=request.group_size,
        group_delay=request.group_delay,
        cancel_existing=True,
        name_style=request.name_style,
        name_base=request.name_base,
    )

    manager.desired_agents = total
    manager.agent_process_manager.sync_next_agent_index()

    return {
        "status": "spawning",
        "total_agents": total,
        "group_size": request.group_size,
        "group_delay": request.group_delay,
        "total_groups": groups,
        "estimated_time_seconds": (groups - 1) * request.group_delay,
        "desired_agents": manager.desired_agents,
    }


@router.post("/swarm/desired")
async def set_desired(request: dict[str, Any], manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    try:
        count = int(request.get("count", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "count must be an integer"}, status_code=400)
    if count < 0:
        return JSONResponse({"error": "count must be >= 0"}, status_code=400)
    manager.desired_agents = count
    manager.agent_process_manager.sync_next_agent_index()
    return {"desired_agents": manager.desired_agents}


@router.post("/swarm/bust-respawn")
async def toggle_bust_respawn(request: dict[str, Any], manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    enabled = bool(request.get("enabled", not manager.bust_respawn))
    manager.bust_respawn = enabled
    await manager.broadcast_status()
    return {"bust_respawn": manager.bust_respawn}


@router.post("/swarm/kill-all")
async def kill_all(manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    return await manager.kill_all()


@router.post("/swarm/clear")
async def clear_swarm(manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    return await manager.clear_swarm()


@router.post("/swarm/prune")
async def prune_dead(manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    return await manager.prune_dead()


@router.post("/swarm/pause")
async def pause_swarm(manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    return await manager.pause_swarm()


@router.post("/swarm/resume")
async def resume_swarm(manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    return await manager.resume_swarm()


@router.post("/agent/{agent_id}/pause")
async def pause_agent(agent_id: str, request: Request, manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    if agent_id not in manager.agents:
        return JSONResponse({"error": f"Agent {agent_id} not found"}, status_code=404)
    agent_status = manager.agents[agent_id]
    agent_status.paused = True
    plugin = get_managed_agent_plugin(request)
    # Try local dispatch via plugin if available
    if plugin is not None:
        local_agent, _ = plugin.resolve_local_agent(agent_status)
        if local_agent is not None:
            local_result = await plugin.dispatch_command(local_agent, "pause")
            if "error" in local_result:
                return JSONResponse({"error": str(local_result["error"])}, status_code=400)
            await manager.broadcast_status()
            return _build_action_response(
                agent_id,
                "pause",
                "local_runtime",
                applied=True,
                queued=False,
                result={"paused": True, **local_result},
                state=str(agent_status.state or "unknown"),
                plugin=plugin,
            )
    queued = _queue_manager_command(agent_status, "pause", {})
    await manager.broadcast_status()
    return _build_action_response(
        agent_id,
        "pause",
        "worker_queue",
        applied=False,
        queued=True,
        result={"paused": True, "queued_command": queued},
        state=str(agent_status.state or "unknown"),
        plugin=plugin if plugin else None,
    )


@router.post("/agent/{agent_id}/resume")
async def resume_agent(agent_id: str, request: Request, manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    if agent_id not in manager.agents:
        return JSONResponse({"error": f"Agent {agent_id} not found"}, status_code=404)
    agent_status = manager.agents[agent_id]
    agent_status.paused = False
    plugin = get_managed_agent_plugin(request)
    if plugin is not None:
        local_agent, _ = plugin.resolve_local_agent(agent_status)
        if local_agent is not None:
            local_result = await plugin.dispatch_command(local_agent, "resume")
            if "error" in local_result:
                return JSONResponse({"error": str(local_result["error"])}, status_code=400)
            await manager.broadcast_status()
            return _build_action_response(
                agent_id,
                "resume",
                "local_runtime",
                applied=True,
                queued=False,
                result={"paused": False, **local_result},
                state=str(agent_status.state or "unknown"),
                plugin=plugin,
            )
    queued = _queue_manager_command(agent_status, "resume", {})
    await manager.broadcast_status()
    return _build_action_response(
        agent_id,
        "resume",
        "worker_queue",
        applied=False,
        queued=True,
        result={"paused": False, "queued_command": queued},
        state=str(agent_status.state or "unknown"),
        plugin=plugin if plugin else None,
    )


@router.post("/agent/{agent_id}/restart")
async def restart_agent(agent_id: str, request: Request, manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    if agent_id not in manager.agents:
        return JSONResponse({"error": f"Agent {agent_id} not found"}, status_code=404)
    agent_status = manager.agents[agent_id]
    plugin = get_managed_agent_plugin(request)
    if plugin is not None:
        local_agent, _ = plugin.resolve_local_agent(agent_status)
        if local_agent is not None:
            local_result = await plugin.dispatch_command(local_agent, "restart")
            if "error" in local_result:
                return JSONResponse({"error": str(local_result["error"])}, status_code=400)
            agent_status.paused = False
            agent_status.state = "running"
            await manager.broadcast_status()
            return _build_action_response(
                agent_id,
                "restart",
                "local_runtime",
                applied=True,
                queued=False,
                result=dict(local_result),
                state="running",
                plugin=plugin,
            )
    queued = _queue_manager_command(agent_status, "restart", {})
    agent_status.paused = False
    await manager.broadcast_status()
    return _build_action_response(
        agent_id,
        "restart",
        "worker_queue",
        applied=False,
        queued=True,
        result={"queued_command": queued},
        state=str(agent_status.state or "unknown"),
        plugin=plugin if plugin else None,
    )
