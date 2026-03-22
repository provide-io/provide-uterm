#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Helper coroutines for BotProcessManager.monitor_processes.

Extracted to keep process.py under the 500-line limit.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

from provide.telemetry import get_logger

if TYPE_CHECKING:
    from provide.terminal.manager.process import BotProcessManager

logger = get_logger(__name__)

_STOP_TIMEOUT_S = 5.0


async def _handle_exited_processes(pm: BotProcessManager) -> None:
    """Update state for any bot processes that have exited."""
    async with pm.manager._state_lock:
        exited = [(bid, p) for bid, p in list(pm.manager.processes.items()) if p.poll() is not None]
    for bot_id, process in exited:
        exit_code = process.returncode
        logger.warning("bot_exited", bot_id=bot_id, exit_code=exit_code)
        async with pm.manager._state_lock:
            bot = pm.manager.bots.get(bot_id)
            if bot is None:
                pm.manager.processes.pop(bot_id, None)
                continue
            if exit_code == 0:
                if bot.state == "error" or bot.error_message:
                    bot.state = "error"
                    if not bot.exit_reason:
                        bot.exit_reason = "reported_error_then_exit_0"
                else:
                    bot.state = "completed"
                    bot.completed_at = time.time()
                    bot.stopped_at = time.time()
                    if not bot.exit_reason:
                        bot.exit_reason = "target_reached"
            else:
                bot.state = "error"
                if not bot.exit_reason:
                    bot.exit_reason = f"exit_code_{exit_code}"
                if not bot.error_message:
                    bot.error_message = f"Process exited with code {exit_code}"
                bot.stopped_at = time.time()
            pm.manager.processes.pop(bot_id, None)
        pm.release_bot_account(bot_id)
        await pm.manager.broadcast_status()


async def _handle_heartbeat_timeouts(pm: BotProcessManager) -> None:
    """Detect bots that have not sent a heartbeat and mark them as error."""
    now = time.time()
    heartbeat_timeout = pm.manager.config.heartbeat_timeout_s
    heartbeat_timed_out: list[str] = []
    import subprocess  # noqa: TC003

    heartbeat_stop_requests: list[tuple[str, subprocess.Popen[bytes]]] = []
    async with pm.manager._state_lock:
        for bot in list(pm.manager.bots.values()):
            if bot.state in ("running",) and now - bot.last_update_time > heartbeat_timeout:
                logger.warning("bot_heartbeat_timeout", bot_id=bot.bot_id, timeout_s=heartbeat_timeout)
                bot.state = "error"
                bot.error_message = (
                    f"No heartbeat in {heartbeat_timeout:.0f}s - bot process may have crashed or is stuck"
                )
                bot.error_type = "HeartbeatTimeout"
                bot.error_timestamp = time.time()
                bot.exit_reason = "heartbeat_timeout"
                bot.stopped_at = time.time()
                if (proc := pm.manager.processes.pop(bot.bot_id, None)) is not None:
                    heartbeat_stop_requests.append((bot.bot_id, proc))
                heartbeat_timed_out.append(bot.bot_id)
    for bot_id, proc in heartbeat_stop_requests:
        await pm._stop_process_tree(bot_id=bot_id, process=proc, timeout_s=_STOP_TIMEOUT_S)
    for bid in heartbeat_timed_out:
        pm.release_bot_account(bid)
    if heartbeat_timed_out:
        await pm.manager.broadcast_status()


def _handle_stale_queued(pm: BotProcessManager) -> None:
    """Launch bots that have been queued too long without spawning."""
    now = time.time()
    for bot in list(pm.manager.bots.values()):
        if bot.state != "queued" or bot.pid != 0 or bot.started_at is not None:
            pm._queued_since.pop(bot.bot_id, None)
            continue
        queued_since = pm._queued_since.get(bot.bot_id)
        if queued_since is None:
            pm._queued_since[bot.bot_id] = now
            continue
        if now - queued_since >= pm._queued_launch_delay:
            if pm.manager.desired_bots > 0:
                pm._queued_since.pop(bot.bot_id, None)
                continue
            logger.warning("stale_queued_bot_launching", bot_id=bot.bot_id, queued_s=round(now - queued_since))
            pm._queued_since.pop(bot.bot_id, None)
            if bot.config:
                task = asyncio.create_task(pm._launch_queued_bot(bot.bot_id, bot.config))
                pm._spawn_tasks.append(task)
            else:
                bot.state = "stopped"
                bot.exit_reason = "no_config"


async def _handle_bust_respawn(pm: BotProcessManager) -> None:
    """Kill bots that are in bust context if bust_respawn is enabled."""
    if not pm.manager.bust_respawn or pm.manager.swarm_paused:
        return
    now = time.time()
    import subprocess  # noqa: TC003

    bust_stop_requests: list[tuple[str, subprocess.Popen[bytes] | None, int | None]] = []
    for bot in list(pm.manager.bots.values()):
        if bot.state != "running":
            continue
        ctx = str(getattr(bot, "activity_context", "") or "").upper()
        if ctx != "BUST":
            continue
        logger.info("bust_respawn_killing_bot", bot_id=bot.bot_id)
        bot.state = "stopped"
        bot.exit_reason = "bust_respawn"
        bot.stopped_at = now
        proc = pm.manager.processes.pop(bot.bot_id, None)
        pid = bot.pid if bot.pid and bot.pid > 0 else None
        bust_stop_requests.append((bot.bot_id, proc, pid))
        pm.release_bot_account(bot.bot_id)
    for bot_id, proc, pid in bust_stop_requests:
        await pm._stop_process_tree(bot_id=bot_id, process=proc, pid=pid, timeout_s=_STOP_TIMEOUT_S)
    await pm.manager.broadcast_status()


async def _handle_desired_state(pm: BotProcessManager) -> None:
    """Enforce the desired bot count: spawn deficits, kill excesses."""
    if pm.manager.desired_bots <= 0 or pm.manager.swarm_paused:
        return
    import subprocess  # noqa: TC003

    active_states = {"running", "queued", "recovering", "blocked"}
    terminal_states = {"error", "stopped", "completed"}
    prune_stop_requests: list[tuple[str, subprocess.Popen[bytes]]] = []

    async with pm.manager._state_lock:
        dead_bots = [b for b in pm.manager.bots.values() if b.state in terminal_states]
        for dead in dead_bots:
            with contextlib.suppress(OSError, RuntimeError):
                pm.release_bot_account(dead.bot_id)
            if (proc := pm.manager.processes.pop(dead.bot_id, None)) is not None:
                prune_stop_requests.append((dead.bot_id, proc))
            pm.manager.bots.pop(dead.bot_id, None)

        active_bots = [b for b in pm.manager.bots.values() if b.state in active_states]
        active_count = len(active_bots)
        deficit = pm.manager.desired_bots - active_count

    if deficit > 0:
        configs_available = [b.config for b in active_bots if b.config]
        if not configs_available:
            configs_available = [b.config for b in dead_bots if b.config]
        if not configs_available and pm._last_spawn_config:
            configs_available = [pm._last_spawn_config]
        for _ in range(deficit):
            if not configs_available:
                break
            config = configs_available[0]
            async with pm.manager._state_lock:
                new_bot_id = pm.allocate_bot_id()
                if new_bot_id not in pm.manager.bots:  # pragma: no branch
                    pm.manager.bots[new_bot_id] = pm.manager._bot_status_class(
                        bot_id=new_bot_id,
                        pid=0,
                        config=config,
                        state="queued",
                    )
            logger.info(
                "desired_state_spawning",
                bot_id=new_bot_id,
                deficit=deficit,
                desired=pm.manager.desired_bots,
            )
            task = asyncio.create_task(pm._launch_queued_bot(new_bot_id, config))
            pm._spawn_tasks.append(task)

    elif deficit < 0:
        excess = -deficit
        to_kill = sorted(active_bots, key=lambda b: b.bot_id, reverse=True)[:excess]
        for bot in to_kill:
            logger.info("desired_state_killing", bot_id=bot.bot_id, excess=excess, desired=pm.manager.desired_bots)
            with contextlib.suppress(OSError, ProcessLookupError, RuntimeError):
                await pm.manager.kill_bot(bot.bot_id)
            with contextlib.suppress(OSError, RuntimeError):
                pm.release_bot_account(bot.bot_id)
            async with pm.manager._state_lock:
                pm.manager.bots.pop(bot.bot_id, None)
                pm.manager.processes.pop(bot.bot_id, None)
    for bot_id, proc in prune_stop_requests:
        await pm._stop_process_tree(bot_id=bot_id, process=proc, timeout_s=_STOP_TIMEOUT_S)
