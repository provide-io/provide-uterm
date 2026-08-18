#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""MCP tools for managing an agent swarm.

Supports two modes:

- **Direct** (in-process): pass an ``AgentManager`` instance.
- **HTTP** (out-of-process): pass a ``base_url`` for the running manager.

Usage::

    # Register onto an app the caller already owns, e.g. an
    # mcp.server.mcpserver.MCPServer.
    register_manager_tools(app, manager=my_manager)
    register_manager_tools(app, base_url="http://localhost:2272")
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable  # noqa: TC003
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from provide.uterm.manager.core import AgentManager

TOOL_COUNT = 15


class _ToolRegistrar(Protocol):
    """Structural type for an app exposing a tool() decorator factory.

    Matches any app that exposes a tool() decorator factory (e.g.
    mcp.server.mcpserver.MCPServer) without importing a concrete server class
    directly. The permissive *args/**kwargs signature is deliberate: it's
    what makes structural matching accept a concrete class's more specific
    tool() signature.
    """

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...


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


def register_manager_tools(
    app: _ToolRegistrar,
    *,
    manager: AgentManager | None = None,
    base_url: str | None = None,
    on_first_http: Callable[[], Awaitable[None]] | None = None,
    agent_telemetry_fields: frozenset[str] | None = None,
) -> int:
    """Register the generic swarm-management tools onto *app*.

    *app* is typed structurally via :class:`_ToolRegistrar`, not against a
    concrete server class: this function works against any app exposing a
    ``tool()`` decorator factory (e.g. ``mcp.server.mcpserver.MCPServer``).
    Only ``app.tool()`` is used.

    Args:
        app: Server app to register onto. Must expose a ``tool()`` decorator
            factory.
        manager: AgentManager instance for direct in-process calls.
        base_url: HTTP base URL (e.g. ``"http://localhost:2272"``) for
            out-of-process calls.  Ignored when *manager* is provided.
        on_first_http: Async callback invoked once before the first HTTP
            request.  Useful for auto-starting the manager process.
        agent_telemetry_fields: Set of per-agent field names to strip from
            ``swarm_status`` responses when ``include_telemetry=False``.

    Returns:
        The number of tools registered (:data:`TOOL_COUNT`).

    Raises:
        ValueError: If neither *manager* nor *base_url* is provided.
    """
    if manager is None and not base_url:
        raise ValueError("Provide either manager (in-process) or base_url (HTTP)")

    mcp = app

    _http_initialized = False

    async def _http(method: str, path: str, **kw: Any) -> tuple[bool, dict[str, Any]]:
        nonlocal _http_initialized
        if base_url is None:  # pragma: no cover — only reachable when base_url is set
            raise RuntimeError("base_url is required for HTTP mode")
        if not _http_initialized and on_first_http is not None:
            _http_initialized = True
            await on_first_http()
        return await _http_request(base_url, method, path, **kw)

    # ------------------------------------------------------------------
    # Swarm-level tools
    # ------------------------------------------------------------------

    @mcp.tool()
    async def swarm_status(include_telemetry: bool = False) -> dict[str, Any]:
        """Get current swarm status: agent counts by state, desired agents, pause state, uptime.

        Args:
            include_telemetry: Include per-agent application-specific telemetry fields.
                               Defaults to False to keep responses small for large swarms.
        """
        if manager is not None:
            data = manager.get_swarm_status().model_dump()
        else:
            ok, data = await _http("GET", "/swarm/status")
            if not ok:
                return {"error": data.get("error", "Failed to get swarm status")}
        if not include_telemetry and agent_telemetry_fields:
            for agent in data.get("agents", []):
                for field in agent_telemetry_fields:
                    agent.pop(field, None)
        return data

    @mcp.tool()
    async def swarm_spawn_batch(
        config_paths: list[str],
        group_size: int = 5,
        group_delay: float = 12.0,
        name_style: str = "random",
        name_base: str = "",
    ) -> dict[str, Any]:
        """Spawn a batch of agents from config files with staggered groups."""
        from provide.uterm.manager.routes.spawn import _validate_config_path

        if manager is not None:
            # Local mode: enforce the spawn sandbox against the live config dir.
            # In HTTP mode the server owns the sandbox (it knows its own config
            # dir) and validates on receipt, so we do not pre-validate here.
            for path in config_paths:
                try:
                    _validate_config_path(path, config_dir_env=manager.config.spawn_config_dir)
                except ValueError as e:
                    return {"error": str(e)}
            total = len(config_paths)
            await manager.start_spawn_swarm(
                config_paths,
                group_size=group_size,
                group_delay=group_delay,
                cancel_existing=True,
                name_style=name_style,
                name_base=name_base,
            )
            manager.desired_agents = total
            manager.agent_process_manager.sync_next_agent_index()
            groups = (total + group_size - 1) // group_size
            return {
                "status": "spawning",
                "total_agents": total,
                "group_size": group_size,
                "group_delay": group_delay,
                "total_groups": groups,
                "estimated_time_seconds": (groups - 1) * group_delay if groups > 1 else 0,
                "desired_agents": total,
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
        """Pause the entire swarm — marks active agents as paused."""
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
        """Cancel pending spawns and terminate all running agent processes."""
        if manager is not None:
            return await manager.kill_all()
        ok, data = await _http("POST", "/swarm/kill-all")
        return data if ok else {"error": data.get("error", "Failed to kill all")}

    @mcp.tool()
    async def swarm_clear() -> dict[str, Any]:
        """Kill all processes and remove all agent registrations."""
        if manager is not None:
            return await manager.clear_swarm()
        ok, data = await _http("POST", "/swarm/clear")
        return data if ok else {"error": data.get("error", "Failed to clear swarm")}

    @mcp.tool()
    async def swarm_prune() -> dict[str, Any]:
        """Remove agents in terminal states (stopped/error/completed)."""
        if manager is not None:
            return await manager.prune_dead()
        ok, data = await _http("POST", "/swarm/prune")
        return data if ok else {"error": data.get("error", "Failed to prune")}

    @mcp.tool()
    async def swarm_set_desired(count: int) -> dict[str, Any]:
        """Set the desired agent count for auto-scaling enforcement."""
        if manager is not None:
            manager.desired_agents = count
            await manager.broadcast_status()
            return {"desired_agents": count}
        ok, data = await _http("POST", "/swarm/desired", json={"count": count})
        return data if ok else {"error": data.get("error", "Failed to set desired")}

    # ------------------------------------------------------------------
    # Per-agent tools
    # ------------------------------------------------------------------

    @mcp.tool()
    async def agent_list(state: str | None = None) -> dict[str, Any]:
        """List all agents, optionally filtered by state."""
        if manager is not None:
            agents = list(manager.agents.values())
            if state:
                agents = [b for b in agents if b.state == state]
            return {"total": len(agents), "agents": [b.model_dump() for b in agents]}
        params: dict[str, Any] = {}
        if state:
            params["state"] = state
        ok, data = await _http("GET", "/agents", params=params or None)
        return data if ok else {"error": data.get("error", "Failed to list agents")}

    @mcp.tool()
    async def agent_status(agent_id: str) -> dict[str, Any]:
        """Get status of a single agent."""
        if manager is not None:
            agent = manager.agents.get(agent_id)
            if agent is None:
                return {"error": f"Agent {agent_id} not found"}
            return agent.model_dump()
        ok, data = await _http("GET", f"/agent/{agent_id}/status")
        return data if ok else {"error": data.get("error", f"Agent {agent_id} not found")}

    @mcp.tool()
    async def agent_kill(agent_id: str) -> dict[str, Any]:
        """Terminate an agent process and remove it."""
        if manager is not None:
            if agent_id not in manager.agents:
                return {"error": f"Agent {agent_id} not found"}
            agent = manager.agents[agent_id]
            if agent_id in manager.processes:
                await manager.kill_agent(agent_id)
            else:
                agent.state = "stopped"
            if manager.desired_agents > 0:
                manager.desired_agents = max(0, manager.desired_agents - 1)
            await manager.broadcast_status()
            return {"agent_id": agent_id, "action": "kill", "state": agent.state}
        ok, data = await _http("DELETE", f"/agent/{agent_id}")
        return data if ok else {"error": data.get("error", f"Failed to kill {agent_id}")}

    @mcp.tool()
    async def agent_pause(agent_id: str) -> dict[str, Any]:
        """Pause a single agent."""
        if manager is not None:
            agent = manager.agents.get(agent_id)
            if agent is None:
                return {"error": f"Agent {agent_id} not found"}
            agent.paused = True
            await manager.broadcast_status()
            return {"agent_id": agent_id, "action": "pause", "paused": True}
        ok, data = await _http("POST", f"/agent/{agent_id}/pause")
        return data if ok else {"error": data.get("error", f"Failed to pause {agent_id}")}

    @mcp.tool()
    async def agent_resume(agent_id: str) -> dict[str, Any]:
        """Resume a paused agent."""
        if manager is not None:
            agent = manager.agents.get(agent_id)
            if agent is None:
                return {"error": f"Agent {agent_id} not found"}
            agent.paused = False
            await manager.broadcast_status()
            return {"agent_id": agent_id, "action": "resume", "paused": False}
        ok, data = await _http("POST", f"/agent/{agent_id}/resume")
        return data if ok else {"error": data.get("error", f"Failed to resume {agent_id}")}

    @mcp.tool()
    async def agent_restart(agent_id: str) -> dict[str, Any]:
        """Queue a restart command for an agent."""
        if manager is not None:
            from provide.uterm.manager.routes.agent_ops import _queue_manager_command

            agent = manager.agents.get(agent_id)
            if agent is None:
                return {"error": f"Agent {agent_id} not found"}
            queued = _queue_manager_command(agent, "restart", {})
            await manager.broadcast_status()
            return {"agent_id": agent_id, "action": "restart", "queued": True, "command": queued}
        ok, data = await _http("POST", f"/agent/{agent_id}/restart")
        return data if ok else {"error": data.get("error", f"Failed to restart {agent_id}")}

    @mcp.tool()
    async def agent_events(agent_id: str) -> dict[str, Any]:
        """Get recent events (actions, errors, status changes) for an agent."""
        if manager is not None:
            agent = manager.agents.get(agent_id)
            if agent is None:
                return {"error": f"Agent {agent_id} not found"}
            events: list[dict[str, Any]] = [
                {"type": "action", **action} if isinstance(action, dict) else {"type": "action", "name": action}
                for action in getattr(agent, "recent_actions", None) or []
            ]
            if agent.error_message:
                events.append(
                    {
                        "type": "error",
                        "message": agent.error_message,
                        "error_type": getattr(agent, "error_type", None),
                        "timestamp": getattr(agent, "error_timestamp", None),
                    }
                )
            return {"agent_id": agent_id, "state": agent.state, "events": events}
        ok, data = await _http("GET", f"/agent/{agent_id}/events")
        return data if ok else {"error": data.get("error", f"Failed to get events for {agent_id}")}

    return TOOL_COUNT
