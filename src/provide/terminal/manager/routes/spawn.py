#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Swarm spawn, kill, and fleet control API routes."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from provide.telemetry import get_logger

from provide.terminal.manager.models import SpawnBatchRequest  # noqa: TC001
from provide.terminal.manager.routes.bot_ops import _queue_manager_command
from provide.terminal.manager.routes.models import get_managed_bot_plugin, require_manager, router

if TYPE_CHECKING:
    from provide.terminal.manager.core import SwarmManager

logger = get_logger(__name__)


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
    """Build a standardised action response, delegating to plugin if available."""
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
async def spawn(config_path: str, bot_id: str = "", manager: SwarmManager = Depends(require_manager)) -> Any:  # noqa: B008
    try:
        _validate_config_path(config_path)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        if not bot_id:
            bot_id = manager.bot_process_manager.allocate_bot_id()
        else:
            manager.bot_process_manager.note_bot_id(bot_id)
        bot_id = await manager.spawn_bot(config_path, bot_id)
        return {"bot_id": bot_id, "pid": manager.bots[bot_id].pid}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/swarm/spawn-batch")
async def spawn_batch(request: SpawnBatchRequest, manager: SwarmManager = Depends(require_manager)) -> Any:  # noqa: B008
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

    manager.desired_bots = total
    manager.bot_process_manager.sync_next_bot_index()

    return {
        "status": "spawning",
        "total_bots": total,
        "group_size": request.group_size,
        "group_delay": request.group_delay,
        "total_groups": groups,
        "estimated_time_seconds": (groups - 1) * request.group_delay,
        "desired_bots": manager.desired_bots,
    }


@router.post("/swarm/desired")
async def set_desired(request: dict[str, Any], manager: SwarmManager = Depends(require_manager)) -> Any:  # noqa: B008
    try:
        count = int(request.get("count", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "count must be an integer"}, status_code=400)
    if count < 0:
        return JSONResponse({"error": "count must be >= 0"}, status_code=400)
    manager.desired_bots = count
    manager.bot_process_manager.sync_next_bot_index()
    return {"desired_bots": manager.desired_bots}


@router.post("/swarm/bust-respawn")
async def toggle_bust_respawn(request: dict[str, Any], manager: SwarmManager = Depends(require_manager)) -> Any:  # noqa: B008
    enabled = bool(request.get("enabled", not manager.bust_respawn))
    manager.bust_respawn = enabled
    await manager.broadcast_status()
    return {"bust_respawn": manager.bust_respawn}


@router.post("/swarm/kill-all")
async def kill_all(manager: SwarmManager = Depends(require_manager)) -> Any:  # noqa: B008
    await manager.cancel_spawn()
    killed = []
    for bot_id in list(manager.processes.keys()):
        try:
            await manager.kill_bot(bot_id)
            killed.append(bot_id)
        except Exception as e:
            logger.exception("failed_to_kill_bot", bot_id=bot_id, error=str(e))
    return {"killed": killed, "count": len(killed)}


@router.post("/swarm/clear")
async def clear_swarm(manager: SwarmManager = Depends(require_manager)) -> Any:  # noqa: B008
    await manager.cancel_spawn()
    for bot_id in list(manager.processes.keys()):
        with contextlib.suppress(OSError, RuntimeError):
            await manager.kill_bot(bot_id)
    for bot_id in list(manager.bots.keys()):
        with contextlib.suppress(AttributeError, RuntimeError):
            manager.bot_process_manager.release_bot_account(bot_id)
    count = len(manager.bots)
    manager.bots.clear()
    manager.processes.clear()
    await manager.broadcast_status()
    return {"cleared": count}


@router.post("/swarm/prune")
async def prune_dead(manager: SwarmManager = Depends(require_manager)) -> Any:  # noqa: B008
    terminal = {"stopped", "error", "completed"}
    dead_ids = [bid for bid, b in manager.bots.items() if b.state in terminal]
    for bid in dead_ids:
        with contextlib.suppress(AttributeError, RuntimeError):
            manager.bot_process_manager.release_bot_account(bid)
        if bid in manager.processes:
            with contextlib.suppress(OSError, ProcessLookupError):
                manager.processes[bid].kill()
            del manager.processes[bid]
        del manager.bots[bid]
    await manager.broadcast_status()
    return {"pruned": len(dead_ids), "remaining": len(manager.bots)}


@router.post("/swarm/pause")
async def pause_swarm(manager: SwarmManager = Depends(require_manager)) -> Any:  # noqa: B008
    manager.swarm_paused = True
    for bot in manager.bots.values():
        if bot.state in {"running", "recovering", "blocked"}:
            bot.paused = True
    await manager.broadcast_status()
    return {"paused": True, "affected": sum(1 for b in manager.bots.values() if b.paused)}


@router.post("/swarm/resume")
async def resume_swarm(manager: SwarmManager = Depends(require_manager)) -> Any:  # noqa: B008
    manager.swarm_paused = False
    resumed = 0
    for bot in manager.bots.values():
        if bot.paused:
            bot.paused = False
            resumed += 1
    await manager.broadcast_status()
    return {"paused": False, "resumed": resumed}


@router.post("/bot/{bot_id}/pause")
async def pause_bot(bot_id: str, request: Request, manager: SwarmManager = Depends(require_manager)) -> Any:  # noqa: B008
    if bot_id not in manager.bots:
        return JSONResponse({"error": f"Bot {bot_id} not found"}, status_code=404)
    bot_status = manager.bots[bot_id]
    bot_status.paused = True
    plugin = get_managed_bot_plugin(request)
    # Try local dispatch via plugin if available
    if plugin is not None:
        local_bot, _ = plugin.resolve_local_bot(bot_status)
        if local_bot is not None:
            local_result = await plugin.dispatch_command(local_bot, "pause")
            if "error" in local_result:
                return JSONResponse({"error": str(local_result["error"])}, status_code=400)
            await manager.broadcast_status()
            return _build_action_response(
                bot_id,
                "pause",
                "local_runtime",
                applied=True,
                queued=False,
                result={"paused": True, **local_result},
                state=str(bot_status.state or "unknown"),
                plugin=plugin,
            )
    queued = _queue_manager_command(bot_status, "pause", {})
    await manager.broadcast_status()
    return _build_action_response(
        bot_id,
        "pause",
        "worker_queue",
        applied=False,
        queued=True,
        result={"paused": True, "queued_command": queued},
        state=str(bot_status.state or "unknown"),
        plugin=plugin if plugin else None,
    )


@router.post("/bot/{bot_id}/resume")
async def resume_bot(bot_id: str, request: Request, manager: SwarmManager = Depends(require_manager)) -> Any:  # noqa: B008
    if bot_id not in manager.bots:
        return JSONResponse({"error": f"Bot {bot_id} not found"}, status_code=404)
    bot_status = manager.bots[bot_id]
    bot_status.paused = False
    plugin = get_managed_bot_plugin(request)
    if plugin is not None:
        local_bot, _ = plugin.resolve_local_bot(bot_status)
        if local_bot is not None:
            local_result = await plugin.dispatch_command(local_bot, "resume")
            if "error" in local_result:
                return JSONResponse({"error": str(local_result["error"])}, status_code=400)
            await manager.broadcast_status()
            return _build_action_response(
                bot_id,
                "resume",
                "local_runtime",
                applied=True,
                queued=False,
                result={"paused": False, **local_result},
                state=str(bot_status.state or "unknown"),
                plugin=plugin,
            )
    queued = _queue_manager_command(bot_status, "resume", {})
    await manager.broadcast_status()
    return _build_action_response(
        bot_id,
        "resume",
        "worker_queue",
        applied=False,
        queued=True,
        result={"paused": False, "queued_command": queued},
        state=str(bot_status.state or "unknown"),
        plugin=plugin if plugin else None,
    )


@router.post("/bot/{bot_id}/restart")
async def restart_bot(bot_id: str, request: Request, manager: SwarmManager = Depends(require_manager)) -> Any:  # noqa: B008
    if bot_id not in manager.bots:
        return JSONResponse({"error": f"Bot {bot_id} not found"}, status_code=404)
    bot_status = manager.bots[bot_id]
    plugin = get_managed_bot_plugin(request)
    if plugin is not None:
        local_bot, _ = plugin.resolve_local_bot(bot_status)
        if local_bot is not None:
            local_result = await plugin.dispatch_command(local_bot, "restart")
            if "error" in local_result:
                return JSONResponse({"error": str(local_result["error"])}, status_code=400)
            bot_status.paused = False
            bot_status.state = "running"
            await manager.broadcast_status()
            return _build_action_response(
                bot_id,
                "restart",
                "local_runtime",
                applied=True,
                queued=False,
                result=dict(local_result),
                state="running",
                plugin=plugin,
            )
    queued = _queue_manager_command(bot_status, "restart", {})
    bot_status.paused = False
    await manager.broadcast_status()
    return _build_action_response(
        bot_id,
        "restart",
        "worker_queue",
        applied=False,
        queued=True,
        result={"queued_command": queued},
        state=str(bot_status.state or "unknown"),
        plugin=plugin if plugin else None,
    )
