#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""AgentManager — generic central coordinator for agent swarm orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi import WebSocket  # noqa: TC002
from provide.telemetry import get_logger

from provide.terminal.manager.constants import SAVE_INTERVAL_S, TIMESERIES_INTERVAL_S
from provide.terminal.manager.models import AgentStatusBase, SwarmStatus
from provide.terminal.manager.timeseries import TimeseriesManager

if TYPE_CHECKING:
    import subprocess

    from provide.terminal.manager.config import ManagerConfig
    from provide.terminal.manager.process import AgentProcessManager
    from provide.terminal.manager.protocols import (
        AccountPoolPlugin,
        IdentityStorePlugin,
        StatusUpdatePlugin,
        TimeseriesPlugin,
    )

logger = get_logger(__name__)


class AgentManager:
    """Generic coordinator for an agent swarm.

    Game-specific behaviour is injected via plugin instances.
    """

    def __init__(
        self,
        config: ManagerConfig,
        *,
        agent_status_class: type[AgentStatusBase] | None = None,
        account_pool: AccountPoolPlugin | None = None,
        identity_store: IdentityStorePlugin | None = None,
        status_update: StatusUpdatePlugin | None = None,
        timeseries_plugin: TimeseriesPlugin | None = None,
        swarm_status_builder: Any | None = None,
    ):
        self.config = config
        self._agent_status_class: type[AgentStatusBase] = agent_status_class or AgentStatusBase
        self.max_agents = config.max_agents
        self.state_file = config.state_file
        self.health_check_interval = config.health_check_interval_s
        self.start_time = time.time()

        self.agents: dict[str, AgentStatusBase] = {}
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.websocket_clients: set[WebSocket] = set()
        self.desired_agents: int = 0
        self.swarm_paused: bool = False
        self.bust_respawn: bool = False

        # Plugin slots
        self.account_pool = account_pool
        self.identity_store = identity_store
        self._status_update_plugin = status_update
        self._swarm_status_builder = swarm_status_builder

        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._state_lock = asyncio.Lock()
        self._ws_lock = asyncio.Lock()

        # MCP client tracking for auto-shutdown.
        self.mcp_clients: set[WebSocket] = set()
        self._mcp_shutdown_task: asyncio.Task[Any] | None = None
        self._server: Any = None  # uvicorn.Server, set during run()

        self.timeseries_manager = TimeseriesManager(
            self.get_swarm_status,
            timeseries_dir=config.timeseries_dir or "logs/metrics",
            interval_s=config.timeseries_interval_s or TIMESERIES_INTERVAL_S,
            plugin=timeseries_plugin,
        )

        # Set by create_manager_app() after construction.
        self.app: Any = None

        # AgentProcessManager set after construction to break circular dep.
        self.agent_process_manager: AgentProcessManager = None  # type: ignore[assignment]

    # --- Delegate process management ---

    async def cancel_spawn(self) -> bool:
        return await self.agent_process_manager.cancel_spawn()

    async def start_spawn_swarm(
        self,
        config_paths: list[str],
        *,
        group_size: int = 1,
        group_delay: float = 12.0,
        cancel_existing: bool = True,
        name_style: str = "random",
        name_base: str = "",
    ) -> None:
        await self.agent_process_manager.start_spawn_swarm(
            config_paths,
            group_size=group_size,
            group_delay=group_delay,
            cancel_existing=cancel_existing,
            name_style=name_style,
            name_base=name_base,
        )

    async def spawn_agent(self, config_path: str, agent_id: str) -> str:
        return await self.agent_process_manager.spawn_agent(config_path, agent_id)

    async def spawn_swarm(
        self,
        config_paths: list[str],
        group_size: int = 5,
        group_delay: float = 60.0,
    ) -> list[str]:
        return await self.agent_process_manager.spawn_swarm(config_paths, group_size, group_delay)

    async def kill_agent(self, agent_id: str) -> None:
        await self.agent_process_manager.kill_agent(agent_id)

    # --- Fleet operations ---

    async def kill_all(self) -> dict[str, Any]:
        """Cancel pending spawns and kill all running agent processes."""
        await self.cancel_spawn()
        killed: list[str] = []
        for agent_id in list(self.processes.keys()):
            try:
                await self.kill_agent(agent_id)
                killed.append(agent_id)
            except Exception:
                logger.exception("failed_to_kill_agent", agent_id=agent_id)
        return {"killed": killed, "count": len(killed)}

    async def clear_swarm(self) -> dict[str, Any]:
        """Kill all processes and remove all agent registrations."""
        await self.cancel_spawn()
        for agent_id in list(self.processes.keys()):
            with contextlib.suppress(OSError, RuntimeError):
                await self.kill_agent(agent_id)
        for agent_id in list(self.agents.keys()):
            with contextlib.suppress(AttributeError, RuntimeError):
                self.agent_process_manager.release_agent_account(agent_id)
        count = len(self.agents)
        self.agents.clear()
        self.processes.clear()
        await self.broadcast_status()
        return {"cleared": count}

    async def prune_dead(self) -> dict[str, Any]:
        """Remove agents in terminal states (stopped/error/completed)."""
        terminal = {"stopped", "error", "completed"}
        dead_ids = [bid for bid, b in self.agents.items() if b.state in terminal]
        for bid in dead_ids:
            with contextlib.suppress(AttributeError, RuntimeError):
                self.agent_process_manager.release_agent_account(bid)
            if bid in self.processes:
                with contextlib.suppress(OSError, ProcessLookupError, RuntimeError):
                    await self.kill_agent(bid)
                self.processes.pop(bid, None)
            del self.agents[bid]
        await self.broadcast_status()
        return {"pruned": len(dead_ids), "remaining": len(self.agents)}

    async def pause_swarm(self) -> dict[str, Any]:
        """Pause the swarm and mark active agents as paused."""
        self.swarm_paused = True
        for agent in self.agents.values():
            if agent.state in {"running", "recovering", "blocked"}:
                agent.paused = True
        await self.broadcast_status()
        return {"paused": True, "affected": sum(1 for b in self.agents.values() if b.paused)}

    async def resume_swarm(self) -> dict[str, Any]:
        """Resume the swarm and unset pause flag on all agents."""
        self.swarm_paused = False
        resumed = 0
        for agent in self.agents.values():
            if agent.paused:
                agent.paused = False
                resumed += 1
        await self.broadcast_status()
        return {"paused": False, "resumed": resumed}

    # --- MCP client lifecycle ---

    async def register_mcp_client(self, ws: WebSocket) -> None:
        """Register an MCP client connection for auto-shutdown tracking."""
        async with self._ws_lock:
            self.mcp_clients.add(ws)
        if self._mcp_shutdown_task is not None:
            self._mcp_shutdown_task.cancel()
            self._mcp_shutdown_task = None
            logger.info("auto_shutdown_cancelled", reason="mcp_client_connected")
        logger.info("mcp_client_registered", total=len(self.mcp_clients))

    async def unregister_mcp_client(self, ws: WebSocket) -> None:
        """Unregister an MCP client and check auto-shutdown conditions."""
        async with self._ws_lock:
            self.mcp_clients.discard(ws)
        remaining = len(self.mcp_clients)
        logger.info("mcp_client_unregistered", remaining=remaining)
        if remaining == 0:
            await self._check_auto_shutdown()

    async def _check_auto_shutdown(self) -> None:
        """Start graceful shutdown timer if conditions are met."""
        if not self.config.auto_shutdown_enabled:
            return
        if self.mcp_clients:
            return
        active = {"running", "queued", "recovering", "blocked"}
        if any(b.state in active for b in self.agents.values()):
            logger.info("auto_shutdown_deferred", reason="active_agents")
            return
        if self._mcp_shutdown_task is not None:
            return
        grace = self.config.auto_shutdown_grace_s
        logger.info("auto_shutdown_scheduled", grace_s=grace)
        self._mcp_shutdown_task = asyncio.create_task(self._auto_shutdown_after(grace))

    async def _auto_shutdown_after(self, grace_s: float) -> None:
        """Wait for grace period, then shut down if conditions still hold."""
        try:
            await asyncio.sleep(grace_s)
        except asyncio.CancelledError:
            return
        if self.mcp_clients:
            return
        active = {"running", "queued", "recovering", "blocked"}
        if any(b.state in active for b in self.agents.values()):
            logger.info("auto_shutdown_aborted", reason="active_agents_during_grace")
            return
        logger.info("auto_shutdown_executing")
        if self._server is not None:  # pragma: no cover (BrPart: coverage artifact with assignment instrumentation)
            self._server.should_exit = True

    # --- Delegate timeseries ---

    def get_timeseries_info(self) -> dict[str, Any]:
        return self.timeseries_manager.get_info()

    def get_timeseries_recent(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.timeseries_manager.get_recent(limit)

    def get_timeseries_summary(self, window_minutes: int = 120) -> dict[str, Any]:
        return self.timeseries_manager.get_summary(window_minutes)

    # --- Swarm status ---

    def get_swarm_status(self) -> SwarmStatus:
        """Build the current swarm status snapshot.

        If *swarm_status_builder* was supplied, delegates to it;
        otherwise builds a base-only status.
        """
        if self._swarm_status_builder is not None:
            return cast("SwarmStatus", self._swarm_status_builder(self))

        agents = list(self.agents.values())
        return SwarmStatus(
            total_agents=len(agents),
            running=sum(1 for b in agents if b.state in ("running", "recovering", "blocked")),
            completed=sum(1 for b in agents if b.state == "completed"),
            errors=sum(1 for b in agents if b.state in ("error", "disconnected", "blocked")),
            stopped=sum(1 for b in agents if b.state == "stopped"),
            uptime_seconds=time.time() - self.start_time,
            timeseries_file=str(self.timeseries_manager.path),
            timeseries_interval_seconds=self.timeseries_manager.interval_s,
            timeseries_samples=self.timeseries_manager.samples_count,
            swarm_paused=self.swarm_paused,
            bust_respawn=self.bust_respawn,
            desired_agents=self.desired_agents,
            agents=list(agents),
        )

    # --- WebSocket broadcasting ---

    async def broadcast_status(self) -> None:
        """Push current status to all connected dashboard WebSocket clients."""
        status = self.get_swarm_status()
        message = status.model_dump_json()

        async with self._ws_lock:
            clients = list(self.websocket_clients)

        disconnected = set()
        for client in clients:
            try:
                await client.send_text(message)
            except Exception:
                disconnected.add(client)

        if disconnected:
            async with self._ws_lock:
                self.websocket_clients -= disconnected

    # Backward-compat alias used by routes
    _broadcast_status = broadcast_status

    # --- State persistence ---

    def _write_state(self, state: dict[str, Any]) -> None:
        """Write state dict to disk atomically (safe from a thread)."""
        state_path = Path(self.state_file)
        tmp = state_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            tmp.replace(state_path)
        except (OSError, ValueError) as exc:
            logger.exception("state_save_failed", error=str(exc), state_file=self.state_file)
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    def _restore_agent(self, agent_id: str, agent_data: dict[str, Any]) -> None:
        """Restore one agent from saved state if not already present."""
        if agent_id not in self.agents:
            saved_state = agent_data.get("state", "stopped")
            if saved_state in ("running", "disconnected", "queued"):
                agent_data["state"] = "stopped"
            if "agent_id" not in agent_data:
                agent_data["agent_id"] = agent_id
            self.agents[agent_id] = self._agent_status_class.model_validate(agent_data)

    def _load_state(self) -> None:
        """Load swarm state from file if it exists."""
        if not self.state_file or not Path(self.state_file).exists():
            return
        try:
            with Path(self.state_file).open() as f:
                state = json.load(f)
                if "desired_agents" in state:
                    self.desired_agents = int(state["desired_agents"] or 0)
                if "swarm_paused" in state:
                    self.swarm_paused = bool(state["swarm_paused"])
                if "bust_respawn" in state:
                    self.bust_respawn = bool(state["bust_respawn"])
                for agent_id, agent_data in state.get("agents", {}).items():
                    try:
                        self._restore_agent(agent_id, agent_data)
                    except Exception as agent_err:
                        logger.warning("agent_state_load_skipped", agent_id=agent_id, error=str(agent_err))
                logger.info(
                    "agents_loaded_from_state",
                    count=len(self.agents),
                    desired_agents=self.desired_agents,
                    swarm_paused=self.swarm_paused,
                    state_file=self.state_file,
                )
        except Exception as e:
            logger.exception("state_load_failed", error=str(e))

    # --- Server lifecycle ---

    async def run(self, host: str | None = None, port: int | None = None) -> None:
        """Start the manager server with background monitoring tasks."""
        import uvicorn

        _host = host or self.config.host
        _port = port or self.config.port
        logger.info("swarm_manager_starting", host=_host, port=_port)

        def _hold(task: asyncio.Task[Any]) -> None:
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        _hold(asyncio.create_task(self.agent_process_manager.monitor_processes()))
        _hold(asyncio.create_task(self.timeseries_manager.loop()))

        async def save_periodically() -> None:
            while True:
                await asyncio.sleep(self.config.save_interval_s or SAVE_INTERVAL_S)
                state = {
                    "timestamp": time.time(),
                    "desired_agents": self.desired_agents,
                    "swarm_paused": self.swarm_paused,
                    "bust_respawn": self.bust_respawn,
                    "agents": {bid: agent.model_dump() for bid, agent in self.agents.items()},
                }
                await asyncio.to_thread(self._write_state, state)

        _hold(asyncio.create_task(save_periodically()))

        config = uvicorn.Config(
            self.app,
            host=_host,
            port=_port,
            log_level=self.config.log_level.lower(),
        )
        server = uvicorn.Server(config)
        self._server = server
        try:
            await server.serve()
        finally:
            hub = getattr(self, "term_hub", None)
            if hub is not None:
                await hub.shutdown()
