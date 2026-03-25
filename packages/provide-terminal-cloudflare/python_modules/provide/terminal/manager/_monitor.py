#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Helper coroutines for AgentProcessManager.monitor_processes.

Extracted to keep process.py under the 500-line limit.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger

if TYPE_CHECKING:
    import subprocess

    from provide.terminal.manager.process import AgentProcessManager

logger = get_logger(__name__)

_STOP_TIMEOUT_S = 5.0


def _set_agent_exit_state(agent: Any, exit_code: int) -> None:
    """Update agent state fields based on process exit code."""
    if exit_code == 0:
        if agent.state == "error" or agent.error_message:
            agent.state = "error"
            if not agent.exit_reason:
                agent.exit_reason = "reported_error_then_exit_0"
        else:
            agent.state = "completed"
            agent.completed_at = time.time()
            agent.stopped_at = time.time()
            if not agent.exit_reason:
                agent.exit_reason = "target_reached"
    else:
        agent.state = "error"
        if not agent.exit_reason:
            agent.exit_reason = f"exit_code_{exit_code}"
        if not agent.error_message:
            agent.error_message = f"Process exited with code {exit_code}"
        agent.stopped_at = time.time()


async def _prune_terminal_agents(
    pm: AgentProcessManager,
) -> tuple[list[tuple[str, subprocess.Popen[bytes]]], list[Any]]:
    """Remove terminal-state agents; return (stop_requests, dead_agents)."""

    terminal_states = {"error", "stopped", "completed"}
    prune_stop_requests: list[tuple[str, subprocess.Popen[bytes]]] = []
    async with pm.manager._state_lock:
        dead_agents = [b for b in pm.manager.agents.values() if b.state in terminal_states]
        for dead in dead_agents:
            with contextlib.suppress(OSError, RuntimeError):
                pm.release_agent_account(dead.agent_id)
            if (proc := pm.manager.processes.pop(dead.agent_id, None)) is not None:
                prune_stop_requests.append((dead.agent_id, proc))
            pm.manager.agents.pop(dead.agent_id, None)
    return prune_stop_requests, dead_agents


def _collect_spawn_configs(
    active_agents: list[Any],
    dead_agents: list[Any],
    last_config: Any,
) -> list[Any]:
    """Collect available spawn configs from active agents, dead agents, or last known config."""
    configs = [b.config for b in active_agents if b.config]
    if not configs:
        configs = [b.config for b in dead_agents if b.config]
    if not configs and last_config:
        configs = [last_config]
    return configs


async def _handle_exited_processes(pm: AgentProcessManager) -> None:
    """Update state for any agent processes that have exited."""
    async with pm.manager._state_lock:
        exited = [(bid, p) for bid, p in list(pm.manager.processes.items()) if p.poll() is not None]
    for agent_id, process in exited:
        exit_code = process.returncode
        logger.warning("agent_exited", agent_id=agent_id, exit_code=exit_code)
        async with pm.manager._state_lock:
            agent = pm.manager.agents.get(agent_id)
            if agent is None:
                pm.manager.processes.pop(agent_id, None)
                continue
            _set_agent_exit_state(agent, exit_code)
            pm.manager.processes.pop(agent_id, None)
        pm.release_agent_account(agent_id)
        await pm.manager.broadcast_status()


async def _handle_heartbeat_timeouts(pm: AgentProcessManager) -> None:
    """Detect agents that have not sent a heartbeat and mark them as error."""
    now = time.time()
    heartbeat_timeout = pm.manager.config.heartbeat_timeout_s
    heartbeat_timed_out: list[str] = []

    heartbeat_stop_requests: list[tuple[str, subprocess.Popen[bytes]]] = []
    async with pm.manager._state_lock:
        for agent in list(pm.manager.agents.values()):
            if agent.state in ("running",) and now - agent.last_update_time > heartbeat_timeout:
                logger.warning("agent_heartbeat_timeout", agent_id=agent.agent_id, timeout_s=heartbeat_timeout)
                agent.state = "error"
                agent.error_message = (
                    f"No heartbeat in {heartbeat_timeout:.0f}s - agent process may have crashed or is stuck"
                )
                agent.error_type = "HeartbeatTimeout"
                agent.error_timestamp = time.time()
                agent.exit_reason = "heartbeat_timeout"
                agent.stopped_at = time.time()
                if (proc := pm.manager.processes.pop(agent.agent_id, None)) is not None:
                    heartbeat_stop_requests.append((agent.agent_id, proc))
                heartbeat_timed_out.append(agent.agent_id)
    for agent_id, proc in heartbeat_stop_requests:
        await pm._stop_process_tree(agent_id=agent_id, process=proc, timeout_s=_STOP_TIMEOUT_S)
    for bid in heartbeat_timed_out:
        pm.release_agent_account(bid)
    if heartbeat_timed_out:
        await pm.manager.broadcast_status()


def _handle_stale_queued(pm: AgentProcessManager) -> None:
    """Launch agents that have been queued too long without spawning."""
    now = time.time()
    for agent in list(pm.manager.agents.values()):
        if agent.state != "queued" or agent.pid != 0 or agent.started_at is not None:
            pm._queued_since.pop(agent.agent_id, None)
            continue
        queued_since = pm._queued_since.get(agent.agent_id)
        if queued_since is None:
            pm._queued_since[agent.agent_id] = now
            continue
        if now - queued_since >= pm._queued_launch_delay:
            if pm.manager.desired_agents > 0:
                pm._queued_since.pop(agent.agent_id, None)
                continue
            logger.warning("stale_queued_agent_launching", agent_id=agent.agent_id, queued_s=round(now - queued_since))
            pm._queued_since.pop(agent.agent_id, None)
            if agent.config:
                task = asyncio.create_task(pm._launch_queued_agent(agent.agent_id, agent.config))
                pm._spawn_tasks.append(task)
            else:
                agent.state = "stopped"
                agent.exit_reason = "no_config"


async def _handle_bust_respawn(pm: AgentProcessManager) -> None:
    """Kill agents that are in bust context if bust_respawn is enabled."""
    if not pm.manager.bust_respawn or pm.manager.swarm_paused:
        return
    now = time.time()

    bust_stop_requests: list[tuple[str, subprocess.Popen[bytes] | None, int | None]] = []
    for agent in list(pm.manager.agents.values()):
        if agent.state != "running":
            continue
        ctx = str(getattr(agent, "activity_context", "") or "").upper()
        if ctx != "BUST":
            continue
        logger.info("bust_respawn_killing_agent", agent_id=agent.agent_id)
        agent.state = "stopped"
        agent.exit_reason = "bust_respawn"
        agent.stopped_at = now
        proc = pm.manager.processes.pop(agent.agent_id, None)
        pid = agent.pid if agent.pid and agent.pid > 0 else None
        bust_stop_requests.append((agent.agent_id, proc, pid))
        pm.release_agent_account(agent.agent_id)
    for agent_id, proc, pid in bust_stop_requests:
        await pm._stop_process_tree(agent_id=agent_id, process=proc, pid=pid, timeout_s=_STOP_TIMEOUT_S)
    await pm.manager.broadcast_status()


async def _spawn_to_desired(
    pm: AgentProcessManager,
    deficit: int,
    active_agents: list[Any],
    dead_agents: list[Any],
) -> None:
    """Spawn *deficit* new agents to reach the desired count."""
    configs_available = _collect_spawn_configs(active_agents, dead_agents, pm._last_spawn_config)
    for _ in range(deficit):
        if not configs_available:
            break
        config = configs_available[0]
        async with pm.manager._state_lock:
            new_agent_id = pm.allocate_agent_id()
            if new_agent_id not in pm.manager.agents:  # pragma: no branch
                pm.manager.agents[new_agent_id] = pm.manager._agent_status_class(
                    agent_id=new_agent_id,
                    pid=0,
                    config=config,
                    state="queued",
                )
        logger.info(
            "desired_state_spawning",
            agent_id=new_agent_id,
            deficit=deficit,
            desired=pm.manager.desired_agents,
        )
        task = asyncio.create_task(pm._launch_queued_agent(new_agent_id, config))
        pm._spawn_tasks.append(task)


async def _kill_excess(pm: AgentProcessManager, excess: int, active_agents: list[Any]) -> None:
    """Kill *excess* agents to trim down to the desired count."""
    to_kill = sorted(active_agents, key=lambda b: b.agent_id, reverse=True)[:excess]
    for agent in to_kill:
        logger.info("desired_state_killing", agent_id=agent.agent_id, excess=excess, desired=pm.manager.desired_agents)
        with contextlib.suppress(OSError, ProcessLookupError, RuntimeError):
            await pm.manager.kill_agent(agent.agent_id)
        with contextlib.suppress(OSError, RuntimeError):
            pm.release_agent_account(agent.agent_id)
        async with pm.manager._state_lock:
            pm.manager.agents.pop(agent.agent_id, None)
            pm.manager.processes.pop(agent.agent_id, None)


async def _handle_desired_state(pm: AgentProcessManager) -> None:
    """Enforce the desired agent count: spawn deficits, kill excesses."""
    if pm.manager.desired_agents <= 0 or pm.manager.swarm_paused:
        return
    active_states = {"running", "queued", "recovering", "blocked"}

    prune_stop_requests, dead_agents = await _prune_terminal_agents(pm)
    async with pm.manager._state_lock:
        active_agents = [b for b in pm.manager.agents.values() if b.state in active_states]
        deficit = pm.manager.desired_agents - len(active_agents)

    if deficit > 0:
        await _spawn_to_desired(pm, deficit, active_agents, dead_agents)
    elif deficit < 0:
        await _kill_excess(pm, -deficit, active_agents)
    for agent_id, proc in prune_stop_requests:
        await pm._stop_process_tree(agent_id=agent_id, process=proc, timeout_s=_STOP_TIMEOUT_S)
