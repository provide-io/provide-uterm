#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Low-level spawn/stop primitives split out of ``process_impl.py``.

Holds the BODIES of ``AgentProcessManager``'s mutable spawn/stop instance
methods as module-level functions (each takes the manager ``self`` as the first
argument); the class keeps thin wrappers that forward here. Splitting these out
keeps both modules under 500 LOC while preserving the public import surface
(``from provide.uterm.manager.process_impl import AgentProcessManager`` and
every method on it are unchanged).

Mutation-enforced at killed==100 alongside ``process_impl.py`` (see
[tool.mutmut].source_paths). The six ``@staticmethod`` helpers and the two
``asyncio.sleep``-driven methods (``spawn_swarm``/``monitor_processes``) stay on
the class in ``process_impl.py`` — the staticmethods are mutmut-skipped via the
decorator and the sleep users rely on the conftest patching
``process_impl.asyncio.sleep`` by module path; both would regress if moved here.
The manager-dir conftest blanket-mocks ``subprocess.Popen`` / ``os.killpg`` /
``os.getpgid`` on the stdlib modules (shared with this module's namespace), so
the moved ``_spawn_process`` / ``_stop_process_tree`` still get those mocks
without a conftest change.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger

from provide.uterm.manager._monitor import _STOP_TIMEOUT_S
from provide.uterm.manager.ext import EVENT_AGENT_SPAWNED

if TYPE_CHECKING:
    from provide.uterm.manager.process_impl import AgentProcessManager, _PopenPlatformKwargs

logger = get_logger(__name__)

__all__ = [
    "_build_preexec_rlimit_fn",
    "_spawn_platform_kwargs",
    "_spawn_process",
    "_stop_process_tree",
    "spawn_agent",
]


async def spawn_agent(self: AgentProcessManager, config_path: str, agent_id: str) -> str:
    self.note_agent_id(agent_id)
    if len(self.manager.agents) >= self.manager.max_agents:
        raise RuntimeError(f"Max agents ({self.manager.max_agents}) reached")
    if not Path(config_path).exists():
        raise RuntimeError(f"Config not found: {config_path}")

    logger.info("spawning_agent", agent_id=agent_id, config_path=config_path)

    worker_type, raw = await self._load_worker_type(config_path)

    # Policy Check
    if not await self._policy_gate.intercept_spawn(agent_id, config_path, raw):
        logger.warning("agent_spawn_rejected_by_policy", agent_id=agent_id)
        raise RuntimeError(f"Spawn rejected by policy for agent {agent_id}")

    registry_entry = self._get_registry_entry(worker_type, config_path)
    worker_module = registry_entry.worker_module

    cmd = [sys.executable, "-m", worker_module, "--config", config_path, "--agent-id", agent_id]

    try:
        env_prefix = self.manager.config.worker_env_prefix
        agent_entry = self.manager.agents.get(agent_id)
        env = self._build_worker_env(env_prefix, agent_entry, registry_entry, raw, agent_id)

        process = await asyncio.to_thread(self._spawn_process, agent_id, cmd, env)

        async with self.manager._state_lock:
            if agent_id in self.manager.agents:
                self.manager.agents[agent_id].pid = process.pid
                self.manager.agents[agent_id].state = "running"
                self.manager.agents[agent_id].last_update_time = time.time()
                self.manager.agents[agent_id].started_at = time.time()
                self.manager.agents[agent_id].stopped_at = None
            else:
                # ``last_update_time`` defaults to 0.0 on the model
                # (see manager/models.py). Without seeding it here the
                # heartbeat monitor (_monitor.py:_handle_heartbeat_timeouts)
                # sees ``now - 0.0`` on its very first tick and immediately
                # marks the just-spawned agent as crashed against the 60s
                # timeout, before the worker can register its first
                # heartbeat. Reproduces deterministically when the agent
                # dict entry was removed (e.g. via ``DELETE /agent/{id}``)
                # between kill and respawn; with a leftover entry the
                # ``if agent_id in self.manager.agents`` branch above
                # would otherwise have set this correctly. Seed to
                # ``time.time()`` to match the update-branch behavior and
                # give the worker a full heartbeat window to phone home.
                self.manager.agents[agent_id] = self.manager._agent_status_class(
                    agent_id=agent_id,
                    pid=process.pid,
                    config=config_path,
                    state="running",
                    started_at=time.time(),
                    last_update_time=time.time(),
                )
            self.manager.processes[agent_id] = process

        self._last_spawn_config = config_path
        logger.info(EVENT_AGENT_SPAWNED, agent_id=agent_id, pid=process.pid, worker_type=worker_type)
        await self.manager.broadcast_status()
        return agent_id

    except Exception as e:
        logger.exception("agent_spawn_failed", agent_id=agent_id, error=str(e))
        raise RuntimeError(f"Failed to spawn agent: {e}") from e


def _spawn_process(
    self: AgentProcessManager, agent_id: str, cmd: list[str], env: dict[str, str]
) -> subprocess.Popen[bytes]:
    from provide.uterm.manager.constants import WORKER_LOG_MAX_BYTES

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


def _spawn_platform_kwargs(self: AgentProcessManager) -> _PopenPlatformKwargs:
    if os.name == "nt":
        flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        return {"creationflags": flags} if flags else {}
    kwargs: _PopenPlatformKwargs = {"start_new_session": True}
    preexec = self._build_preexec_rlimit_fn()
    if preexec is not None:
        kwargs["preexec_fn"] = preexec
    return kwargs


def _build_preexec_rlimit_fn(self: AgentProcessManager) -> Any | None:
    """Return a preexec function that applies configured worker resource limits."""
    if os.name == "nt":
        return None
    try:
        import resource
    except Exception:
        return None

    cfg = self.manager.config
    limit_specs: list[tuple[int, int, int]] = []

    nofile_soft = int(cfg.worker_rlimit_nofile_soft or 0)
    nofile_hard = int(cfg.worker_rlimit_nofile_hard or 0)
    if (nofile_soft > 0 or nofile_hard > 0) and hasattr(resource, "RLIMIT_NOFILE"):
        if nofile_soft <= 0:
            nofile_soft = nofile_hard
        if nofile_hard <= 0:
            nofile_hard = nofile_soft
        limit_specs.append((int(resource.RLIMIT_NOFILE), nofile_soft, nofile_hard))

    as_mb = int(cfg.worker_rlimit_as_mb or 0)
    if as_mb > 0 and hasattr(resource, "RLIMIT_AS"):
        as_bytes = as_mb * 1024 * 1024
        limit_specs.append((int(resource.RLIMIT_AS), as_bytes, as_bytes))

    cpu_s = int(cfg.worker_rlimit_cpu_s or 0)
    if cpu_s > 0 and hasattr(resource, "RLIMIT_CPU"):
        limit_specs.append((int(resource.RLIMIT_CPU), cpu_s, cpu_s))

    if not limit_specs:
        return None

    def _apply_limits() -> None:
        for res_kind, soft, hard in limit_specs:
            resource.setrlimit(res_kind, (soft, hard))

    return _apply_limits


async def _stop_process_tree(
    self: AgentProcessManager,
    *,
    agent_id: str,
    process: subprocess.Popen[bytes] | None = None,
    pid: int | None = None,
    timeout_s: float = _STOP_TIMEOUT_S,
) -> None:
    resolved_pid = self._resolve_stop_pid(process, pid)
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
