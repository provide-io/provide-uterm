#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""SwarmManager — generic central coordinator for bot swarm orchestration."""

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
from provide.terminal.manager.models import BotStatusBase, SwarmStatus
from provide.terminal.manager.timeseries import TimeseriesManager

if TYPE_CHECKING:
    import subprocess

    from provide.terminal.manager.config import ManagerConfig
    from provide.terminal.manager.process import BotProcessManager
    from provide.terminal.manager.protocols import (
        AccountPoolPlugin,
        IdentityStorePlugin,
        StatusUpdatePlugin,
        TimeseriesPlugin,
    )

logger = get_logger(__name__)


class SwarmManager:
    """Generic coordinator for a bot swarm.

    Game-specific behaviour is injected via plugin instances.
    """

    def __init__(
        self,
        config: ManagerConfig,
        *,
        bot_status_class: type[BotStatusBase] | None = None,
        account_pool: AccountPoolPlugin | None = None,
        identity_store: IdentityStorePlugin | None = None,
        status_update: StatusUpdatePlugin | None = None,
        timeseries_plugin: TimeseriesPlugin | None = None,
        swarm_status_builder: Any | None = None,
    ):
        self.config = config
        self._bot_status_class: type[BotStatusBase] = bot_status_class or BotStatusBase
        self.max_bots = config.max_bots
        self.state_file = config.state_file
        self.health_check_interval = config.health_check_interval_s
        self.start_time = time.time()

        self.bots: dict[str, BotStatusBase] = {}
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.websocket_clients: set[WebSocket] = set()
        self.desired_bots: int = 0
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

        self.timeseries_manager = TimeseriesManager(
            self.get_swarm_status,
            timeseries_dir=config.timeseries_dir or "logs/metrics",
            interval_s=config.timeseries_interval_s or TIMESERIES_INTERVAL_S,
            plugin=timeseries_plugin,
        )

        # Set by create_manager_app() after construction.
        self.app: Any = None

        # BotProcessManager set after construction to break circular dep.
        self.bot_process_manager: BotProcessManager = None  # type: ignore[assignment]

    # --- Delegate process management ---

    async def cancel_spawn(self) -> bool:
        return await self.bot_process_manager.cancel_spawn()

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
        await self.bot_process_manager.start_spawn_swarm(
            config_paths,
            group_size=group_size,
            group_delay=group_delay,
            cancel_existing=cancel_existing,
            name_style=name_style,
            name_base=name_base,
        )

    async def spawn_bot(self, config_path: str, bot_id: str) -> str:
        return await self.bot_process_manager.spawn_bot(config_path, bot_id)

    async def spawn_swarm(
        self,
        config_paths: list[str],
        group_size: int = 5,
        group_delay: float = 60.0,
    ) -> list[str]:
        return await self.bot_process_manager.spawn_swarm(config_paths, group_size, group_delay)

    async def kill_bot(self, bot_id: str) -> None:
        await self.bot_process_manager.kill_bot(bot_id)

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

        bots = list(self.bots.values())
        return SwarmStatus(
            total_bots=len(bots),
            running=sum(1 for b in bots if b.state in ("running", "recovering", "blocked")),
            completed=sum(1 for b in bots if b.state == "completed"),
            errors=sum(1 for b in bots if b.state in ("error", "disconnected", "blocked")),
            stopped=sum(1 for b in bots if b.state == "stopped"),
            uptime_seconds=time.time() - self.start_time,
            timeseries_file=str(self.timeseries_manager.path),
            timeseries_interval_seconds=self.timeseries_manager.interval_s,
            timeseries_samples=self.timeseries_manager.samples_count,
            swarm_paused=self.swarm_paused,
            bust_respawn=self.bust_respawn,
            desired_bots=self.desired_bots,
            bots=[b.model_dump() for b in bots],
        )

    # --- WebSocket broadcasting ---

    async def broadcast_status(self) -> None:
        """Push current status to all connected dashboard WebSocket clients."""
        status = self.get_swarm_status()
        message = json.dumps(status.model_dump())

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

    def _load_state(self) -> None:
        """Load swarm state from file if it exists."""
        if not self.state_file or not Path(self.state_file).exists():
            return
        try:
            with Path(self.state_file).open() as f:
                state = json.load(f)
                if "desired_bots" in state:
                    self.desired_bots = int(state["desired_bots"] or 0)
                if "swarm_paused" in state:
                    self.swarm_paused = bool(state["swarm_paused"])
                if "bust_respawn" in state:
                    self.bust_respawn = bool(state["bust_respawn"])
                for bot_id, bot_data in state.get("bots", {}).items():
                    try:
                        if bot_id not in self.bots:
                            saved_state = bot_data.get("state", "stopped")
                            if saved_state in ("running", "disconnected", "queued"):
                                bot_data["state"] = "stopped"
                            if "bot_id" not in bot_data:
                                bot_data["bot_id"] = bot_id
                            self.bots[bot_id] = self._bot_status_class.model_validate(bot_data)
                    except Exception as bot_err:
                        logger.warning("bot_state_load_skipped", bot_id=bot_id, error=str(bot_err))
                logger.info(
                    "bots_loaded_from_state",
                    count=len(self.bots),
                    desired_bots=self.desired_bots,
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

        _hold(asyncio.create_task(self.bot_process_manager.monitor_processes()))
        _hold(asyncio.create_task(self.timeseries_manager.loop()))

        async def save_periodically() -> None:
            while True:
                await asyncio.sleep(self.config.save_interval_s or SAVE_INTERVAL_S)
                state = {
                    "timestamp": time.time(),
                    "desired_bots": self.desired_bots,
                    "swarm_paused": self.swarm_paused,
                    "bust_respawn": self.bust_respawn,
                    "bots": {bid: bot.model_dump() for bid, bot in self.bots.items()},
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
        try:
            await server.serve()
        finally:
            hub = getattr(self, "term_hub", None)
            if hub is not None:
                await hub.shutdown()
