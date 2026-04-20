#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Agent process management for the generic swarm manager."""

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

import yaml
from provide.telemetry import get_logger

if TYPE_CHECKING:
    from provide.terminal.manager.core import AgentManager
    from provide.terminal.manager.protocols import WorkerRegistryPlugin

from provide.terminal.manager._monitor import (
    _STOP_TIMEOUT_S,
    _cleanup_old_worker_logs,
    _handle_bust_respawn,
    _handle_desired_state,
    _handle_exited_processes,
    _handle_heartbeat_timeouts,
    _handle_stale_queued,
)

logger = get_logger(__name__)
_AGENT_ID_RE = re.compile(r"^agent_(\d+)$")


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


class AgentProcessManager:
    """Manages agent process spawning, monitoring, and termination."""

    def __init__(
        self,
        manager: AgentManager,
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
        self._next_agent_index: int = 0
        self._spawn_name_style: str = "random"
        self._spawn_name_base: str = ""
        self._last_spawn_config: str | None = None
        self._try_set_subreaper()

    @staticmethod
    def _try_set_subreaper() -> None:
        """Mark this process as a subreaper on Linux.

        When set, orphaned grandchild processes are reparented to us instead of
        init, ensuring ``_stop_process_tree`` can reap them even if the direct
        child called ``setsid()`` or otherwise changed its process group.
        Best-effort — silently ignored on non-Linux or unprivileged systems.
        """
        if sys.platform != "linux":
            return
        try:
            import ctypes

            pr_set_child_subreaper = 36
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            rc = libc.prctl(pr_set_child_subreaper, 1, 0, 0, 0)
            if rc == 0:
                logger.debug("subreaper_set")
        except Exception:  # noqa: S110
            pass  # not available — fall back to killpg-only

    @staticmethod
    def _parse_agent_index(agent_id: str) -> int | None:
        match = _AGENT_ID_RE.match(str(agent_id or "").strip())
        if not match:
            return None
        return int(match.group(1))

    def sync_next_agent_index(self) -> int:
        max_seen = -1
        for known_id in set(self.manager.agents) | set(self.manager.processes):
            idx = self._parse_agent_index(known_id)
            if idx is not None:
                max_seen = max(max_seen, idx)
        self._next_agent_index = max(self._next_agent_index, max_seen + 1)
        return self._next_agent_index

    def note_agent_id(self, agent_id: str) -> None:
        idx = self._parse_agent_index(agent_id)
        if idx is None:
            return
        self._next_agent_index = max(self._next_agent_index, idx + 1)

    def allocate_agent_id(self) -> str:
        idx = self.sync_next_agent_index()
        while True:
            candidate = f"agent_{idx:03d}"
            if candidate not in self.manager.agents and candidate not in self.manager.processes:
                self._next_agent_index = idx + 1
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

    async def _load_worker_type(self, config_path: str) -> tuple[str, dict[str, Any]]:
        """Load worker_type and raw config dict from a YAML config file."""
        raw: dict[str, Any] = {}
        try:
            raw_text = await asyncio.to_thread(Path(config_path).read_text)
            raw = yaml.safe_load(raw_text) or {}
            worker_type = str(raw.get("worker_type", "default") or "default")
        except Exception as exc:
            logger.warning("worker_type_read_failed", config_path=config_path, error=str(exc))
            worker_type = "default"
        return worker_type, raw

    def _get_registry_entry(self, worker_type: str, config_path: str) -> Any:
        """Resolve the worker registry entry for *worker_type*.

        Falls back to the sole registered entry when worker_type is 'default'.
        """
        registry_entry = self._worker_registry.get(worker_type)
        if registry_entry is None:
            if len(self._worker_registry) == 1 and worker_type == "default":
                return next(iter(self._worker_registry.values()))
            raise RuntimeError(
                f"Unknown worker_type {worker_type!r} in {config_path}. Registered: {sorted(self._worker_registry)}"
            )
        return registry_entry

    def _build_worker_env(
        self,
        env_prefix: str,
        agent_entry: Any,
        registry_entry: Any,
        raw_config: dict[str, Any],
    ) -> dict[str, str]:
        """Build the environment dict for a worker subprocess."""
        env = {k: v for k, v in os.environ.items() if k.startswith(env_prefix) or k in _WORKER_ENV_PASSTHROUGH}
        if self._spawn_name_style:
            env[f"{env_prefix}NAME_STYLE"] = self._spawn_name_style
        if self._spawn_name_base:
            env[f"{env_prefix}NAME_BASE"] = self._spawn_name_base
        if agent_entry is not None:
            registry_entry.configure_worker_env(env, agent_entry, self.manager, raw_config=raw_config)
        return env

    async def spawn_agent(self, config_path: str, agent_id: str) -> str:
        self.note_agent_id(agent_id)
        if len(self.manager.agents) >= self.manager.max_agents:
            raise RuntimeError(f"Max agents ({self.manager.max_agents}) reached")
        if not Path(config_path).exists():
            raise RuntimeError(f"Config not found: {config_path}")

        logger.info("spawning_agent", agent_id=agent_id, config_path=config_path)

        worker_type, raw = await self._load_worker_type(config_path)
        registry_entry = self._get_registry_entry(worker_type, config_path)
        worker_module = registry_entry.worker_module

        cmd = [sys.executable, "-m", worker_module, "--config", config_path, "--agent-id", agent_id]

        try:
            env_prefix = self.manager.config.worker_env_prefix
            agent_entry = self.manager.agents.get(agent_id)
            env = self._build_worker_env(env_prefix, agent_entry, registry_entry, raw)

            process = await asyncio.to_thread(self._spawn_process, agent_id, cmd, env)

            async with self.manager._state_lock:
                if agent_id in self.manager.agents:
                    self.manager.agents[agent_id].pid = process.pid
                    self.manager.agents[agent_id].state = "running"
                    self.manager.agents[agent_id].last_update_time = time.time()
                    self.manager.agents[agent_id].started_at = time.time()
                    self.manager.agents[agent_id].stopped_at = None
                else:
                    self.manager.agents[agent_id] = self.manager._agent_status_class(
                        agent_id=agent_id,
                        pid=process.pid,
                        config=config_path,
                        state="running",
                        started_at=time.time(),
                    )
                self.manager.processes[agent_id] = process

            self._last_spawn_config = config_path
            logger.info("agent_spawned", agent_id=agent_id, pid=process.pid)
            await self.manager.broadcast_status()
            return agent_id

        except Exception as e:
            logger.exception("agent_spawn_failed", agent_id=agent_id, error=str(e))
            raise RuntimeError(f"Failed to spawn agent: {e}") from e

    def _spawn_process(self, agent_id: str, cmd: list[str], env: dict[str, str]) -> subprocess.Popen[bytes]:
        from provide.terminal.manager.constants import WORKER_LOG_MAX_BYTES

        log_dir = Path(self._log_dir) if self._log_dir else Path("logs/workers")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{agent_id}.log"
        # Rotate oversized log from previous lifecycle.
        if log_file.is_file():
            with contextlib.suppress(OSError):
                if log_file.stat().st_size > WORKER_LOG_MAX_BYTES:
                    prev = log_dir / f"{agent_id}.log.prev"
                    prev.unlink(missing_ok=True)
                    log_file.rename(prev)
        log_handle = log_file.open("w")
        try:
            proc = subprocess.Popen(
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
        agent_id: str,
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
            logger.warning("agent_force_killed", agent_id=agent_id)
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
            logger.info("agent_terminated", agent_id=agent_id)
            return
        except TimeoutError:
            pass

        if os.name != "nt":
            with contextlib.suppress(OSError, ProcessLookupError):
                self._signal_posix_process_group(resolved_pid, signal.SIGKILL)
        with contextlib.suppress(TimeoutError, OSError, RuntimeError):
            await self._wait_for_process_exit(process, 1.0)
        logger.warning("agent_force_killed", agent_id=agent_id)

    async def spawn_swarm(
        self,
        config_paths: list[str],
        group_size: int = 5,
        group_delay: float = 60.0,
        name_style: str = "random",
        name_base: str = "",
    ) -> list[str]:
        agent_ids: list[str] = []
        total = len(config_paths)

        self._spawn_name_style = name_style
        self._spawn_name_base = name_base

        # Pre-register all agents as queued.
        async with self.manager._state_lock:
            base_index = self.sync_next_agent_index()
            self._next_agent_index = base_index + total
        for i, config in enumerate(config_paths):
            agent_id = f"agent_{base_index + i:03d}"
            if agent_id not in self.manager.agents:  # pragma: no branch
                self.manager.agents[agent_id] = self.manager._agent_status_class(
                    agent_id=agent_id,
                    pid=0,
                    config=config,
                    state="queued",
                )
        await self.manager.broadcast_status()

        for group_start in range(0, total, group_size):
            group_end = min(group_start + group_size, total)
            group_configs = config_paths[group_start:group_end]

            for i, config in enumerate(group_configs):
                bid = f"agent_{base_index + group_start + i:03d}"
                try:
                    await self.spawn_agent(config, bid)
                    agent_ids.append(bid)
                except Exception as e:
                    logger.exception("agent_spawn_failed_in_group", agent_id=bid, config=config, error=str(e))

            if group_end < total:
                await asyncio.sleep(group_delay)

        logger.info("swarm_spawn_complete", started=len(agent_ids), total=total)
        return agent_ids

    async def kill_agent(self, agent_id: str) -> None:
        async with self.manager._state_lock:
            process = self.manager.processes.get(agent_id)
            agent = self.manager.agents.get(agent_id)
            fallback_pid = int(getattr(agent, "pid", 0) or 0) if process is None else 0

        await self._stop_process_tree(
            agent_id=agent_id,
            process=process,
            pid=fallback_pid or None,
            timeout_s=_STOP_TIMEOUT_S,
        )

        async with self.manager._state_lock:
            if agent_id in self.manager.agents:
                self.manager.agents[agent_id].state = "stopped"
                self.manager.agents[agent_id].stopped_at = time.time()
            self.manager.processes.pop(agent_id, None)
        self.release_agent_account(agent_id)
        await self.manager.broadcast_status()

    def release_agent_account(self, agent_id: str) -> None:
        pool = self.manager.account_pool
        if pool is None:
            return
        try:
            released = pool.release_by_agent(agent_id=agent_id, cooldown_s=0)
            if released:
                logger.info("manager_released_account", agent_id=agent_id)
        except Exception as e:
            logger.warning("account_release_failed", agent_id=agent_id, error=str(e))

    async def _launch_queued_agent(self, agent_id: str, config: str) -> None:
        try:
            await self.spawn_agent(config, agent_id)
        except Exception as e:
            logger.exception("stale_queued_agent_launch_failed", agent_id=agent_id, error=str(e))
            if agent_id in self.manager.agents:
                self.manager.agents[agent_id].state = "error"
                self.manager.agents[agent_id].error_message = f"Launch failed: {e}"
                self.manager.agents[agent_id].exit_reason = "launch_failed"
            await self.manager.broadcast_status()

    async def monitor_processes(self) -> None:
        """Monitor agent processes for crashes or completion."""
        _monitor_iter = 0
        while True:
            await _handle_exited_processes(self)
            self._spawn_tasks = [t for t in self._spawn_tasks if not t.done()]
            await _handle_heartbeat_timeouts(self)
            _handle_stale_queued(self)
            await _handle_bust_respawn(self)
            await _handle_desired_state(self)
            _monitor_iter += 1
            if _monitor_iter % 360 == 0:  # ~1 hour at 10s interval
                with contextlib.suppress(Exception):
                    _cleanup_old_worker_logs(self)
            await asyncio.sleep(self.manager.health_check_interval)
