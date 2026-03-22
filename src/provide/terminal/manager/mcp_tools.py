#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FastMCP tools for managing a bot swarm.

Supports two modes:

- **Direct** (in-process): pass a ``SwarmManager`` instance.
- **HTTP** (out-of-process): pass a ``base_url`` for the running manager.

Usage::

    # In-process
    mcp = create_manager_mcp_tools(manager=my_manager)

    # Out-of-process
    mcp = create_manager_mcp_tools(base_url="http://localhost:2272")
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from provide.terminal.manager.core import SwarmManager

TOOL_COUNT = 15


async def _http_request(base_url: str, method: str, path: str, **kwargs: Any) -> tuple[bool, dict[str, Any]]:
    """Call the manager REST API and normalize the response."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(method, f"{base_url}{path}", **kwargs)
        data = response.json()
        if response.status_code >= 400:
            return False, data if isinstance(data, dict) else {"error": str(data)}
        return True, data if isinstance(data, dict) else {"value": data}
    except Exception as exc:
        return False, {"error": str(exc)}


def create_manager_mcp_tools(
    manager: SwarmManager | None = None,
    *,
    base_url: str | None = None,
) -> FastMCP:
    """Create a FastMCP app with generic swarm management tools.

    Args:
        manager: SwarmManager instance for direct in-process calls.
        base_url: HTTP base URL (e.g. ``"http://localhost:2272"``) for
            out-of-process calls.  Ignored when *manager* is provided.

    Raises:
        ValueError: If neither *manager* nor *base_url* is provided.
    """
    if manager is None and not base_url:
        raise ValueError("Provide either manager (in-process) or base_url (HTTP)")

    from fastmcp import FastMCP as _FastMCP

    mcp = _FastMCP("provide-terminal-manager")

    async def _http(method: str, path: str, **kw: Any) -> tuple[bool, dict[str, Any]]:
        if base_url is None:  # pragma: no cover — only reachable when base_url is set
            raise RuntimeError("base_url is required for HTTP mode")
        return await _http_request(base_url, method, path, **kw)

    # ------------------------------------------------------------------
    # Swarm-level tools
    # ------------------------------------------------------------------

    @mcp.tool()
    async def swarm_status() -> dict[str, Any]:
        """Get current swarm status: bot counts by state, desired bots, pause state, uptime."""
        if manager is not None:
            return manager.get_swarm_status().model_dump()
        ok, data = await _http("GET", "/swarm/status")
        return data if ok else {"error": data.get("error", "Failed to get swarm status")}

    @mcp.tool()
    async def swarm_spawn_batch(
        config_paths: list[str],
        group_size: int = 5,
        group_delay: float = 12.0,
        name_style: str = "random",
        name_base: str = "",
    ) -> dict[str, Any]:
        """Spawn a batch of bots from config files with staggered groups."""
        if manager is not None:
            total = len(config_paths)
            await manager.start_spawn_swarm(
                config_paths,
                group_size=group_size,
                group_delay=group_delay,
                cancel_existing=True,
                name_style=name_style,
                name_base=name_base,
            )
            manager.desired_bots = total
            manager.bot_process_manager.sync_next_bot_index()
            groups = (total + group_size - 1) // group_size
            return {
                "status": "spawning",
                "total_bots": total,
                "group_size": group_size,
                "group_delay": group_delay,
                "total_groups": groups,
                "estimated_time_seconds": (groups - 1) * group_delay if groups > 1 else 0,
                "desired_bots": total,
            }
        ok, data = await _http(
            "POST",
            "/swarm/spawn-batch",
            json={
                "config_paths": config_paths,
                "group_size": group_size,
                "group_delay": group_delay,
                "name_style": name_style,
                "name_base": name_base,
            },
        )
        return data if ok else {"error": data.get("error", "Failed to spawn batch")}

    @mcp.tool()
    async def swarm_pause() -> dict[str, Any]:
        """Pause the entire swarm — marks active bots as paused."""
        if manager is not None:
            return await manager.pause_swarm()
        ok, data = await _http("POST", "/swarm/pause")
        return data if ok else {"error": data.get("error", "Failed to pause swarm")}

    @mcp.tool()
    async def swarm_resume() -> dict[str, Any]:
        """Resume a paused swarm."""
        if manager is not None:
            return await manager.resume_swarm()
        ok, data = await _http("POST", "/swarm/resume")
        return data if ok else {"error": data.get("error", "Failed to resume swarm")}

    @mcp.tool()
    async def swarm_kill_all() -> dict[str, Any]:
        """Cancel pending spawns and terminate all running bot processes."""
        if manager is not None:
            return await manager.kill_all()
        ok, data = await _http("POST", "/swarm/kill-all")
        return data if ok else {"error": data.get("error", "Failed to kill all")}

    @mcp.tool()
    async def swarm_clear() -> dict[str, Any]:
        """Kill all processes and remove all bot registrations."""
        if manager is not None:
            return await manager.clear_swarm()
        ok, data = await _http("POST", "/swarm/clear")
        return data if ok else {"error": data.get("error", "Failed to clear swarm")}

    @mcp.tool()
    async def swarm_prune() -> dict[str, Any]:
        """Remove bots in terminal states (stopped/error/completed)."""
        if manager is not None:
            return await manager.prune_dead()
        ok, data = await _http("POST", "/swarm/prune")
        return data if ok else {"error": data.get("error", "Failed to prune")}

    @mcp.tool()
    async def swarm_set_desired(count: int) -> dict[str, Any]:
        """Set the desired bot count for auto-scaling enforcement."""
        if manager is not None:
            manager.desired_bots = count
            await manager.broadcast_status()
            return {"desired_bots": count}
        ok, data = await _http("POST", "/swarm/desired", json={"count": count})
        return data if ok else {"error": data.get("error", "Failed to set desired")}

    # ------------------------------------------------------------------
    # Per-bot tools
    # ------------------------------------------------------------------

    @mcp.tool()
    async def bot_list(state: str | None = None) -> dict[str, Any]:
        """List all bots, optionally filtered by state."""
        if manager is not None:
            bots = list(manager.bots.values())
            if state:
                bots = [b for b in bots if b.state == state]
            return {"total": len(bots), "bots": [b.model_dump() for b in bots]}
        params: dict[str, Any] = {}
        if state:
            params["state"] = state
        ok, data = await _http("GET", "/bots", params=params or None)
        return data if ok else {"error": data.get("error", "Failed to list bots")}

    @mcp.tool()
    async def bot_status(bot_id: str) -> dict[str, Any]:
        """Get status of a single bot."""
        if manager is not None:
            bot = manager.bots.get(bot_id)
            if bot is None:
                return {"error": f"Bot {bot_id} not found"}
            return bot.model_dump()
        ok, data = await _http("GET", f"/bot/{bot_id}/status")
        return data if ok else {"error": data.get("error", f"Bot {bot_id} not found")}

    @mcp.tool()
    async def bot_kill(bot_id: str) -> dict[str, Any]:
        """Terminate a bot process and remove it."""
        if manager is not None:
            if bot_id not in manager.bots:
                return {"error": f"Bot {bot_id} not found"}
            bot = manager.bots[bot_id]
            if bot_id in manager.processes:
                await manager.kill_bot(bot_id)
            else:
                bot.state = "stopped"
            if manager.desired_bots > 0:
                manager.desired_bots = max(0, manager.desired_bots - 1)
            await manager.broadcast_status()
            return {"bot_id": bot_id, "action": "kill", "state": bot.state}
        ok, data = await _http("DELETE", f"/bot/{bot_id}")
        return data if ok else {"error": data.get("error", f"Failed to kill {bot_id}")}

    @mcp.tool()
    async def bot_pause(bot_id: str) -> dict[str, Any]:
        """Pause a single bot."""
        if manager is not None:
            bot = manager.bots.get(bot_id)
            if bot is None:
                return {"error": f"Bot {bot_id} not found"}
            bot.paused = True
            await manager.broadcast_status()
            return {"bot_id": bot_id, "action": "pause", "paused": True}
        ok, data = await _http("POST", f"/bot/{bot_id}/pause")
        return data if ok else {"error": data.get("error", f"Failed to pause {bot_id}")}

    @mcp.tool()
    async def bot_resume(bot_id: str) -> dict[str, Any]:
        """Resume a paused bot."""
        if manager is not None:
            bot = manager.bots.get(bot_id)
            if bot is None:
                return {"error": f"Bot {bot_id} not found"}
            bot.paused = False
            await manager.broadcast_status()
            return {"bot_id": bot_id, "action": "resume", "paused": False}
        ok, data = await _http("POST", f"/bot/{bot_id}/resume")
        return data if ok else {"error": data.get("error", f"Failed to resume {bot_id}")}

    @mcp.tool()
    async def bot_restart(bot_id: str) -> dict[str, Any]:
        """Queue a restart command for a bot."""
        if manager is not None:
            from provide.terminal.manager.routes.bot_ops import _queue_manager_command

            bot = manager.bots.get(bot_id)
            if bot is None:
                return {"error": f"Bot {bot_id} not found"}
            queued = _queue_manager_command(bot, "restart", {})
            await manager.broadcast_status()
            return {"bot_id": bot_id, "action": "restart", "queued": True, "command": queued}
        ok, data = await _http("POST", f"/bot/{bot_id}/restart")
        return data if ok else {"error": data.get("error", f"Failed to restart {bot_id}")}

    @mcp.tool()
    async def bot_events(bot_id: str) -> dict[str, Any]:
        """Get recent events (actions, errors, status changes) for a bot."""
        if manager is not None:
            bot = manager.bots.get(bot_id)
            if bot is None:
                return {"error": f"Bot {bot_id} not found"}
            events: list[dict[str, Any]] = [
                {"type": "action", **action} if isinstance(action, dict) else {"type": "action", "name": action}
                for action in getattr(bot, "recent_actions", None) or []
            ]
            if bot.error_message:
                events.append(
                    {
                        "type": "error",
                        "message": bot.error_message,
                        "error_type": getattr(bot, "error_type", None),
                        "timestamp": getattr(bot, "error_timestamp", None),
                    }
                )
            return {"bot_id": bot_id, "state": bot.state, "events": events}
        ok, data = await _http("GET", f"/bot/{bot_id}/events")
        return data if ok else {"error": data.get("error", f"Failed to get events for {bot_id}")}

    return mcp
