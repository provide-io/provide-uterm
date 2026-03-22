#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Bot process management for the generic swarm manager."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

import yaml  # type: ignore[import-untyped]
from provide.telemetry import get_logger

if TYPE_CHECKING:
    from provide.terminal.manager.core import SwarmManager
    from provide.terminal.manager.protocols import WorkerRegistryPlugin

from provide.terminal.manager._monitor import (
    _STOP_TIMEOUT_S,
    _handle_bust_respawn,
    _handle_desired_state,
    _handle_exited_processes,
    _handle_heartbeat_timeouts,
    _handle_stale_queued,
)

logger = get_logger(__name__)
_BOT_ID_RE = re.compile(r"^bot_(\d+)$")


class _PopenPlatformKwargs(TypedDict, total=False):
    creationflags: int
    start_new_session: bool


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
                **self._spawn_platform_kwargs(),
            )
        except Exception:
            log_handle.close()
            raise
        log_handle.close()
        return proc

    @staticmethod
    def _spawn_platform_kwargs() -> _PopenPlatformKwargs:
        if os.name == "nt":
            flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            return {"creationflags": flags} if flags else {}
        return {"start_new_session": True}

    @staticmethod
    async def _wait_for_process_exit(process: subprocess.Popen[bytes], timeout_s: float) -> None:
        wait_fn = process.wait
        if inspect.iscoroutinefunction(wait_fn):
            await asyncio.wait_for(cast("Any", wait_fn)(), timeout=timeout_s)
            return
        loop = asyncio.get_running_loop()
        result = await asyncio.wait_for(loop.run_in_executor(None, wait_fn), timeout=timeout_s)
        if inspect.isawaitable(result):
            await asyncio.wait_for(result, timeout=timeout_s)

    @staticmethod
    def _signal_posix_process_group(pid: int, sig: signal.Signals) -> None:
        pgid = os.getpgid(pid)
        os.killpg(pgid, sig)

    @staticmethod
    async def _taskkill_process_tree(pid: int) -> None:
        proc = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    async def _stop_process_tree(
        self,
        *,
        bot_id: str,
        process: subprocess.Popen[bytes] | None = None,
        pid: int | None = None,
        timeout_s: float = _STOP_TIMEOUT_S,
    ) -> None:
        resolved_pid = int(pid or getattr(process, "pid", 0) or 0)
        if resolved_pid <= 0:
            return

        if process is None:
            if os.name == "nt":
                with contextlib.suppress(OSError, RuntimeError):
                    await self._taskkill_process_tree(resolved_pid)
            else:
                with contextlib.suppress(OSError, ProcessLookupError):
                    self._signal_posix_process_group(resolved_pid, signal.SIGKILL)
            logger.warning("bot_force_killed", bot_id=bot_id)
            return

        if os.name == "nt":
            # On Windows, terminate() only kills the immediate process, leaving
            # grandchildren running.  taskkill /T /F kills the whole job tree.
            with contextlib.suppress(OSError, RuntimeError):
                await self._taskkill_process_tree(resolved_pid)
        else:
            with contextlib.suppress(OSError, ProcessLookupError):
                self._signal_posix_process_group(resolved_pid, signal.SIGTERM)

        try:
            await self._wait_for_process_exit(process, timeout_s)
            logger.info("bot_terminated", bot_id=bot_id)
            return
        except TimeoutError:
            pass

        if os.name != "nt":
            with contextlib.suppress(OSError, ProcessLookupError):
                self._signal_posix_process_group(resolved_pid, signal.SIGKILL)
        with contextlib.suppress(TimeoutError, OSError, RuntimeError):
            await self._wait_for_process_exit(process, 1.0)
        logger.warning("bot_force_killed", bot_id=bot_id)

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
            if bot_id not in self.manager.bots:  # pragma: no branch
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

        await self._stop_process_tree(bot_id=bot_id, process=process, timeout_s=_STOP_TIMEOUT_S)

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
            await _handle_exited_processes(self)
            self._spawn_tasks = [t for t in self._spawn_tasks if not t.done()]
            await _handle_heartbeat_timeouts(self)
            _handle_stale_queued(self)
            await _handle_bust_respawn(self)
            await _handle_desired_state(self)
            await asyncio.sleep(self.manager.health_check_interval)
