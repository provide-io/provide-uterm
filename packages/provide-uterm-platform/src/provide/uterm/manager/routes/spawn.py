#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Swarm spawn, kill, and fleet control API routes."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from provide.telemetry import get_logger

from provide.uterm.manager.constants import CONFIG_DIR_ENV_VAR, TERMINAL_STATES
from provide.uterm.manager.models import SpawnBatchRequest  # noqa: TC001
from provide.uterm.manager.routes.agent_ops import _build_action_response, _queue_manager_command
from provide.uterm.manager.routes.models import get_managed_agent_plugin, require_manager, router

if TYPE_CHECKING:
    from provide.uterm.manager.core import AgentManager

logger = get_logger(__name__)


@router.get("/health")
async def health_check() -> Any:
    return {"status": "ok"}


def _manager_config_dir() -> str:
    """Return the configured spawn-sandbox base dir from ManagerConfig defaults.

    Kept as a separate seam so the env var and the model default can both be
    consulted without threading a live manager into the module-level validator.
    """
    from provide.uterm.manager.config import ManagerConfig

    return ManagerConfig().spawn_config_dir.strip()


def _validate_config_path(config_path: str, *, config_dir_env: str = "") -> Path:
    """Validate *config_path* is a safe YAML file inside the spawn sandbox.

    A base config dir MUST be configured (via the ``config_dir_env`` argument,
    the ``UTERM_CONFIG_DIR`` env var, or ``ManagerConfig.spawn_config_dir``);
    otherwise the spawn is refused rather than allowing unrestricted traversal.
    Both the candidate path and the base are fully resolved (symlinks followed)
    before the containment check, so a symlinked leaf inside the base that
    points outside is rejected.
    """
    base_raw = config_dir_env or os.environ.get(CONFIG_DIR_ENV_VAR, "").strip() or _manager_config_dir()
    if not base_raw:
        raise ValueError("config dir is not configured; refusing to spawn from an unrestricted path")
    base = Path(os.path.realpath(base_raw))
    resolved = Path(os.path.realpath(config_path))
    if resolved.suffix.lower() not in (".yaml", ".yml"):
        raise ValueError(f"config_path must be a .yaml or .yml file: {config_path}")
    if not resolved.is_relative_to(base):
        raise ValueError(f"config_path is outside config dir ({base}): {config_path}")
    return resolved


@router.post("/swarm/spawn")
async def spawn(config_path: str, agent_id: str = "", manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    try:
        _validate_config_path(config_path, config_dir_env=manager.config.spawn_config_dir)
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
    for path in request.config_paths:
        try:
            _validate_config_path(path, config_dir_env=manager.config.spawn_config_dir)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
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
    # The worker_queue path queues the restart command; the agent reads it
    # on its next status poll, sets stop_reason, and exits cleanly. Without
    # a respawn step here the endpoint's contract ("Restart a bot by
    # killing it and respawning with same config") is half-honored — the
    # bot dies but never comes back. Schedule a watcher to spawn a fresh
    # process from the same config_path once the existing one exits.
    if agent_status.config:
        task = asyncio.create_task(_respawn_after_restart_exit(manager, agent_id, agent_status.config))
        manager._background_tasks.add(task)
        task.add_done_callback(manager._background_tasks.discard)
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


async def _respawn_after_restart_exit(
    manager: AgentManager,
    agent_id: str,
    config_path: str,
    *,
    exit_timeout_s: float = 60.0,
    poll_interval_s: float = 0.5,
) -> None:
    """Wait for *agent_id* to exit (state→completed/error/stopped), then
    re-spawn it from *config_path*.

    Paired with the worker_queue branch of ``/agent/{id}/restart``: that
    branch queues a restart command which the agent reads + obeys by
    exiting cleanly. This watcher closes the loop so the bot actually
    comes back, matching the local-managed plugin path's behavior.
    """
    deadline = time.monotonic() + exit_timeout_s
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_interval_s)
        agent = manager.agents.get(agent_id)
        if agent is None:
            return
        if agent.state in TERMINAL_STATES:
            break
    else:
        logger.warning(
            "respawn_after_restart_exit_timeout",
            agent_id=agent_id,
            timeout_s=exit_timeout_s,
        )
        return
    # Clear the pending command fields so the freshly-respawned process
    # doesn't read the same "restart" command on its first status poll
    # and immediately exit again — the manager's spawn_agent reuses the
    # existing agent record (only resets pid/state/timestamps), so any
    # leftover pending_command_* fields would be served back to the new
    # bot.
    fresh_agent = manager.agents.get(agent_id)
    if fresh_agent is not None:
        fresh_agent.pending_command_seq = 0
        fresh_agent.pending_command_type = None
        fresh_agent.pending_command_payload = {}
    try:
        await manager.spawn_agent(config_path, agent_id)
        logger.info("respawn_after_restart_complete", agent_id=agent_id)
    except Exception as exc:
        logger.warning(
            "respawn_after_restart_failed",
            agent_id=agent_id,
            config_path=config_path,
            error=str(exc),
        )
