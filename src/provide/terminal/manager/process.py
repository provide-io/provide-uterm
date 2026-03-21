#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Bot process management for the generic swarm manager."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml  # type: ignore[import-untyped]
from provide.telemetry import get_logger

if TYPE_CHECKING:
    from provide.terminal.manager.core import SwarmManager
    from provide.terminal.manager.protocols import WorkerRegistryPlugin

logger = get_logger(__name__)
_BOT_ID_RE = re.compile(r"^bot_(\d+)$")

# Allowlist of env vars forwarded to worker subprocesses.
_WORKER_ENV_PASSTHROUGH = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "TMPDIR",
        "TMP",
        "TEMP",
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
    }
)


class BotProcessManager:
    """Manages bot process spawning, monitoring, and termination."""

    def __init__(
        self,
        manager: SwarmManager,
        *,
        worker_registry: dict[str, WorkerRegistryPlugin] | None = None,
        log_dir: str = "",
    ):
        self.manager = manager
        self._worker_registry = worker_registry or {}
        self._log_dir = log_dir
        self._spawn_tasks: list[asyncio.Task[Any]] = []
        self._queued_since: dict[str, float] = {}
        self._queued_launch_delay: float = 30.0
        self._next_bot_index: int = 0
        self._spawn_name_style: str = "random"
        self._spawn_name_base: str = ""
        self._last_spawn_config: str | None = None

    @staticmethod
    def _parse_bot_index(bot_id: str) -> int | None:
        match = _BOT_ID_RE.match(str(bot_id or "").strip())
        if not match:
            return None
        return int(match.group(1))

    def sync_next_bot_index(self) -> int:
        max_seen = -1
        for known_id in set(self.manager.bots) | set(self.manager.processes):
            idx = self._parse_bot_index(known_id)
            if idx is not None:
                max_seen = max(max_seen, idx)
        self._next_bot_index = max(self._next_bot_index, max_seen + 1)
        return self._next_bot_index

    def note_bot_id(self, bot_id: str) -> None:
        idx = self._parse_bot_index(bot_id)
        if idx is None:
            return
        self._next_bot_index = max(self._next_bot_index, idx + 1)

    def allocate_bot_id(self) -> str:
        idx = self.sync_next_bot_index()
        while True:
            candidate = f"bot_{idx:03d}"
            if candidate not in self.manager.bots and candidate not in self.manager.processes:
                self._next_bot_index = idx + 1
                return candidate
            idx += 1

    async def cancel_spawn(self) -> bool:
        tasks = [t for t in self._spawn_tasks if not t.done()]
        self._spawn_tasks = []
        if not tasks:
            return False
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return True

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
        self._spawn_tasks = [t for t in self._spawn_tasks if not t.done()]
        if cancel_existing:
            await self.cancel_spawn()
        task = asyncio.create_task(
            self.spawn_swarm(
                config_paths,
                group_size=group_size,
                group_delay=group_delay,
                name_style=name_style,
                name_base=name_base,
            )
        )
        self._spawn_tasks.append(task)

    async def spawn_bot(self, config_path: str, bot_id: str) -> str:
        self.note_bot_id(bot_id)
        if len(self.manager.bots) >= self.manager.max_bots:
            raise RuntimeError(f"Max bots ({self.manager.max_bots}) reached")
        if not Path(config_path).exists():
            raise RuntimeError(f"Config not found: {config_path}")

        logger.info("spawning_bot", bot_id=bot_id, config_path=config_path)

        worker_type = "default"
        raw: dict[str, Any] = {}
        try:
            raw_text = await asyncio.to_thread(Path(config_path).read_text)
            raw = yaml.safe_load(raw_text) or {}
            worker_type = str(raw.get("worker_type", "default") or "default")
        except Exception as exc:
            logger.warning("worker_type_read_failed", config_path=config_path, error=str(exc))

        registry_entry = self._worker_registry.get(worker_type)
        if registry_entry is None:
            # Single-registry fallback: if only one worker type is registered and the
            # config has no worker_type key, use that sole entry automatically.
            if len(self._worker_registry) == 1 and worker_type == "default":
                registry_entry = next(iter(self._worker_registry.values()))
            else:
                raise RuntimeError(
                    f"Unknown worker_type {worker_type!r} in {config_path}. Registered: {sorted(self._worker_registry)}"
                )
        worker_module = registry_entry.worker_module

        cmd = [sys.executable, "-m", worker_module, "--config", config_path, "--bot-id", bot_id]

        try:
            env_prefix = self.manager.config.worker_env_prefix
            env = {k: v for k, v in os.environ.items() if k.startswith(env_prefix) or k in _WORKER_ENV_PASSTHROUGH}

            bot_entry = self.manager.bots.get(bot_id)
            if self._spawn_name_style:
                env[f"{env_prefix}NAME_STYLE"] = self._spawn_name_style
            if self._spawn_name_base:
                env[f"{env_prefix}NAME_BASE"] = self._spawn_name_base

            # Let the game plugin inject additional env vars (incl. game-specific fields).
            if bot_entry is not None:
                registry_entry.configure_worker_env(env, bot_entry, self.manager, raw_config=raw)

            process = await asyncio.to_thread(self._spawn_process, bot_id, cmd, env)

            async with self.manager._state_lock:
                if bot_id in self.manager.bots:
                    self.manager.bots[bot_id].pid = process.pid
                    self.manager.bots[bot_id].state = "running"
                    self.manager.bots[bot_id].last_update_time = time.time()
                    self.manager.bots[bot_id].started_at = time.time()
                    self.manager.bots[bot_id].stopped_at = None
                else:
                    self.manager.bots[bot_id] = self.manager._bot_status_class(
                        bot_id=bot_id,
                        pid=process.pid,
                        config=config_path,
                        state="running",
                        started_at=time.time(),
                    )
                self.manager.processes[bot_id] = process

            self._last_spawn_config = config_path
            logger.info("bot_spawned", bot_id=bot_id, pid=process.pid)
            await self.manager.broadcast_status()
            return bot_id

        except Exception as e:
            logger.exception("bot_spawn_failed", bot_id=bot_id, error=str(e))
            raise RuntimeError(f"Failed to spawn bot: {e}") from e

    def _spawn_process(self, bot_id: str, cmd: list[str], env: dict[str, str]) -> subprocess.Popen[bytes]:
        log_dir = Path(self._log_dir) if self._log_dir else Path("logs/workers")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{bot_id}.log"
        log_handle = log_file.open("a")
        try:
            proc = subprocess.Popen(  # noqa: S603
                cmd,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=env,
            )
        except Exception:
            log_handle.close()
            raise
        log_handle.close()
        return proc

    async def spawn_swarm(
        self,
        config_paths: list[str],
        group_size: int = 5,
        group_delay: float = 60.0,
        name_style: str = "random",
        name_base: str = "",
    ) -> list[str]:
        bot_ids: list[str] = []
        total = len(config_paths)

        self._spawn_name_style = name_style
        self._spawn_name_base = name_base

        # Pre-register all bots as queued.
        async with self.manager._state_lock:
            base_index = self.sync_next_bot_index()
            self._next_bot_index = base_index + total
        for i, config in enumerate(config_paths):
            bot_id = f"bot_{base_index + i:03d}"
            if bot_id not in self.manager.bots:
                self.manager.bots[bot_id] = self.manager._bot_status_class(
                    bot_id=bot_id,
                    pid=0,
                    config=config,
                    state="queued",
                )
        await self.manager.broadcast_status()

        for group_start in range(0, total, group_size):
            group_end = min(group_start + group_size, total)
            group_configs = config_paths[group_start:group_end]

            for i, config in enumerate(group_configs):
                bid = f"bot_{base_index + group_start + i:03d}"
                try:
                    await self.spawn_bot(config, bid)
                    bot_ids.append(bid)
                except Exception as e:
                    logger.exception("bot_spawn_failed_in_group", bot_id=bid, config=config, error=str(e))

            if group_end < total:
                await asyncio.sleep(group_delay)

        logger.info("swarm_spawn_complete", started=len(bot_ids), total=total)
        return bot_ids

    async def kill_bot(self, bot_id: str) -> None:
        async with self.manager._state_lock:
            if bot_id not in self.manager.processes:
                return
            process = self.manager.processes[bot_id]

        try:
            process.terminate()
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, process.wait),
                timeout=5.0,
            )
            logger.info("bot_terminated", bot_id=bot_id)
        except TimeoutError:
            with contextlib.suppress(OSError, ProcessLookupError):
                process.kill()
            logger.warning("bot_force_killed", bot_id=bot_id)

        async with self.manager._state_lock:
            if bot_id in self.manager.bots:
                self.manager.bots[bot_id].state = "stopped"
                self.manager.bots[bot_id].stopped_at = time.time()
            self.manager.processes.pop(bot_id, None)
        self.release_bot_account(bot_id)
        await self.manager.broadcast_status()

    def release_bot_account(self, bot_id: str) -> None:
        pool = self.manager.account_pool
        if pool is None:
            return
        try:
            released = pool.release_by_bot(bot_id=bot_id, cooldown_s=0)
            if released:
                logger.info("manager_released_account", bot_id=bot_id)
        except Exception as e:
            logger.warning("account_release_failed", bot_id=bot_id, error=str(e))

    async def _launch_queued_bot(self, bot_id: str, config: str) -> None:
        try:
            await self.spawn_bot(config, bot_id)
        except Exception as e:
            logger.exception("stale_queued_bot_launch_failed", bot_id=bot_id, error=str(e))
            if bot_id in self.manager.bots:
                self.manager.bots[bot_id].state = "error"
                self.manager.bots[bot_id].error_message = f"Launch failed: {e}"
                self.manager.bots[bot_id].exit_reason = "launch_failed"
            await self.manager.broadcast_status()

    async def monitor_processes(self) -> None:
        """Monitor bot processes for crashes or completion."""
        while True:
            async with self.manager._state_lock:
                exited = [(bid, p) for bid, p in list(self.manager.processes.items()) if p.poll() is not None]
            for bot_id, process in exited:
                exit_code = process.returncode
                logger.warning("bot_exited", bot_id=bot_id, exit_code=exit_code)
                async with self.manager._state_lock:
                    bot = self.manager.bots.get(bot_id)
                    if bot is None:
                        self.manager.processes.pop(bot_id, None)
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
                    self.manager.processes.pop(bot_id, None)
                self.release_bot_account(bot_id)
                await self.manager.broadcast_status()

            self._spawn_tasks = [t for t in self._spawn_tasks if not t.done()]

            now = time.time()
            heartbeat_timeout = self.manager.config.heartbeat_timeout_s
            heartbeat_timed_out: list[str] = []
            async with self.manager._state_lock:
                for bot in list(self.manager.bots.values()):
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
                        if bot.bot_id in self.manager.processes:
                            with contextlib.suppress(OSError, ProcessLookupError):
                                self.manager.processes[bot.bot_id].kill()
                            self.manager.processes.pop(bot.bot_id, None)
                        heartbeat_timed_out.append(bot.bot_id)
            for bid in heartbeat_timed_out:
                self.release_bot_account(bid)
            if heartbeat_timed_out:
                await self.manager.broadcast_status()

            # Stale-queued detection
            for bot in list(self.manager.bots.values()):
                if bot.state != "queued" or bot.pid != 0 or bot.started_at is not None:
                    self._queued_since.pop(bot.bot_id, None)
                    continue
                queued_since = self._queued_since.get(bot.bot_id)
                if queued_since is None:
                    self._queued_since[bot.bot_id] = now
                    continue
                if now - queued_since >= self._queued_launch_delay:
                    if self.manager.desired_bots > 0:
                        self._queued_since.pop(bot.bot_id, None)
                        continue
                    logger.warning("stale_queued_bot_launching", bot_id=bot.bot_id, queued_s=round(now - queued_since))
                    self._queued_since.pop(bot.bot_id, None)
                    if bot.config:
                        task = asyncio.create_task(self._launch_queued_bot(bot.bot_id, bot.config))
                        self._spawn_tasks.append(task)
                    else:
                        bot.state = "stopped"
                        bot.exit_reason = "no_config"

            # Bust-respawn
            if self.manager.bust_respawn and not self.manager.swarm_paused:
                for bot in list(self.manager.bots.values()):
                    if bot.state != "running":
                        continue
                    ctx = str(getattr(bot, "activity_context", "") or "").upper()
                    if ctx != "BUST":
                        continue
                    logger.info("bust_respawn_killing_bot", bot_id=bot.bot_id)
                    bot.state = "stopped"
                    bot.exit_reason = "bust_respawn"
                    bot.stopped_at = now
                    if bot.bot_id in self.manager.processes:
                        with contextlib.suppress(OSError, ProcessLookupError):
                            self.manager.processes[bot.bot_id].kill()
                        self.manager.processes.pop(bot.bot_id, None)
                    elif bot.pid and bot.pid > 0:
                        with contextlib.suppress(OSError, ProcessLookupError):
                            os.kill(bot.pid, 9)
                    self.release_bot_account(bot.bot_id)
                await self.manager.broadcast_status()

            # Desired-state enforcement
            if self.manager.desired_bots > 0 and not self.manager.swarm_paused:
                active_states = {"running", "queued", "recovering", "blocked"}
                terminal_states = {"error", "stopped", "completed"}

                async with self.manager._state_lock:
                    dead_bots = [b for b in self.manager.bots.values() if b.state in terminal_states]
                    for dead in dead_bots:
                        with contextlib.suppress(OSError, RuntimeError):
                            self.release_bot_account(dead.bot_id)
                        if dead.bot_id in self.manager.processes:
                            with contextlib.suppress(OSError, ProcessLookupError):
                                self.manager.processes[dead.bot_id].kill()
                            self.manager.processes.pop(dead.bot_id, None)
                        self.manager.bots.pop(dead.bot_id, None)

                    active_bots = [b for b in self.manager.bots.values() if b.state in active_states]
                    active_count = len(active_bots)
                    deficit = self.manager.desired_bots - active_count

                if deficit > 0:
                    configs_available = [b.config for b in active_bots if b.config]
                    if not configs_available:
                        configs_available = [b.config for b in dead_bots if b.config]
                    if not configs_available and self._last_spawn_config:
                        configs_available = [self._last_spawn_config]
                    for _ in range(deficit):
                        if not configs_available:
                            break
                        config = configs_available[0]
                        async with self.manager._state_lock:
                            new_bot_id = self.allocate_bot_id()
                            if new_bot_id not in self.manager.bots:
                                self.manager.bots[new_bot_id] = self.manager._bot_status_class(
                                    bot_id=new_bot_id,
                                    pid=0,
                                    config=config,
                                    state="queued",
                                )
                        logger.info(
                            "desired_state_spawning",
                            bot_id=new_bot_id,
                            deficit=deficit,
                            desired=self.manager.desired_bots,
                        )
                        task = asyncio.create_task(self._launch_queued_bot(new_bot_id, config))
                        self._spawn_tasks.append(task)

                elif deficit < 0:
                    excess = -deficit
                    to_kill = sorted(active_bots, key=lambda b: b.bot_id, reverse=True)[:excess]
                    for bot in to_kill:
                        logger.info(
                            "desired_state_killing", bot_id=bot.bot_id, excess=excess, desired=self.manager.desired_bots
                        )
                        with contextlib.suppress(OSError, ProcessLookupError, RuntimeError):
                            await self.manager.kill_bot(bot.bot_id)
                        with contextlib.suppress(OSError, RuntimeError):
                            self.release_bot_account(bot.bot_id)
                        async with self.manager._state_lock:
                            self.manager.bots.pop(bot.bot_id, None)
                            self.manager.processes.pop(bot.bot_id, None)

            await asyncio.sleep(self.manager.health_check_interval)
