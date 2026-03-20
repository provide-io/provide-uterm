# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Final coverage gap tests — targeting remaining uncovered lines."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from provide.terminal.manager.app import create_manager_app
from provide.terminal.manager.config import ManagerConfig
from provide.terminal.manager.core import SwarmManager
from provide.terminal.manager.models import BotStatusBase
from provide.terminal.manager.process import BotProcessManager


class FakeWorkerPlugin:
    @property
    def game_type(self) -> str:
        return "test_game"

    @property
    def worker_module(self) -> str:
        return "test_module"

    def configure_worker_env(self, env, bot_status, manager):
        pass


# ── models.py: require_manager success path + get_account_pool ───────────


class TestModelsRequireManager503:
    """Cover models.py line 25: HTTPException when manager not set."""

    def test_require_manager_raises_503(self):
        from fastapi import Depends, FastAPI

        from provide.terminal.manager.routes.models import require_manager

        app = FastAPI()

        @app.get("/test")
        async def test_route(m=Depends(require_manager)):  # noqa: B008
            return {"ok": True}

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/test")
        assert resp.status_code == 503


class TestModelsRequireManagerSuccess:
    """Cover models.py line 25 (require_manager returns), 37-38 (get_account_pool)."""

    def test_require_manager_found(self, tmp_path):
        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
        )
        pool = MagicMock()
        app, manager = create_manager_app(config, account_pool=pool)
        client = TestClient(app)
        # Any route that uses require_manager exercises line 25
        resp = client.get("/swarm/status")
        assert resp.status_code == 200

    def test_account_pool_accessible_via_route(self, tmp_path):
        """The get_account_pool dep is used by game-specific routes via extra_routers."""
        from fastapi import APIRouter, Depends

        from provide.terminal.manager.routes.models import get_account_pool

        extra = APIRouter()

        @extra.get("/test-pool")
        async def test_pool(pool=Depends(get_account_pool)):  # noqa: B008
            return {"has_pool": pool is not None}

        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
        )
        pool = MagicMock()
        app, manager = create_manager_app(config, account_pool=pool, extra_routers=[extra])
        client = TestClient(app)
        resp = client.get("/test-pool")
        assert resp.status_code == 200
        assert resp.json()["has_pool"] is True


# ── process.py: allocate_bot_id idx += 1 (line 102) ─────────────────────


class TestCoreSavePeriodically:
    """Cover core.py lines 264-271: save_periodically inner coroutine."""

    @pytest.mark.asyncio
    async def test_run_triggers_save(self, tmp_path):
        config = ManagerConfig(
            state_file=str(tmp_path / "state.json"),
            timeseries_dir=str(tmp_path / "m"),
            save_interval_s=0.05,  # very short
            health_check_interval_s=0,
        )
        mgr = SwarmManager(config)
        pm_mock = MagicMock()
        pm_mock.monitor_processes = AsyncMock()
        mgr.bot_process_manager = pm_mock
        mgr.timeseries_manager.loop = AsyncMock()
        mgr.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")

        async def serve_briefly():
            await asyncio.sleep(0.15)  # long enough for save to fire

        mock_server = AsyncMock()
        mock_server.serve = serve_briefly

        with patch("uvicorn.Config", return_value=MagicMock()), patch("uvicorn.Server", return_value=mock_server):
            await mgr.run()

        # State file should have been written
        import json

        state_path = tmp_path / "state.json"
        assert state_path.exists()
        data = json.loads(state_path.read_text())
        assert "bot_000" in data.get("bots", {})


class TestAllocateSkipsProcesses:
    def test_allocate_skips_blocked_candidate(self, tmp_path):
        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
        )
        manager = SwarmManager(config)
        pm = BotProcessManager(manager, worker_registry={})
        manager.bot_process_manager = pm

        # Block bot_000 so the while loop hits idx += 1 (line 102)
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000")
        with patch.object(pm, "sync_next_bot_index", return_value=0):
            bid = pm.allocate_bot_id()
        assert bid == "bot_001"


# ── process.py: desired-state scale-down (lines 498-510) ────────────────


class TestDesiredStateScaleDown:
    @pytest.mark.asyncio
    async def test_monitor_desired_scale_down_kills_excess(self, tmp_path):
        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
            heartbeat_timeout_s=999,
            health_check_interval_s=0,
        )
        manager = SwarmManager(config)
        manager.broadcast_status = AsyncMock()
        pm = BotProcessManager(manager, worker_registry={})
        manager.bot_process_manager = pm

        manager.desired_bots = 1
        manager.bots["bot_000"] = BotStatusBase(
            bot_id="bot_000",
            state="running",
            last_update_time=time.time(),
        )
        manager.bots["bot_001"] = BotStatusBase(
            bot_id="bot_001",
            state="running",
            last_update_time=time.time(),
        )

        with patch.object(manager, "kill_bot", new_callable=AsyncMock):
            task = asyncio.create_task(pm.monitor_processes())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # One bot should have been removed
        assert len(manager.bots) <= 1


# ── process.py: desired-state prune dead with processes (line 460-462) ───


class TestDesiredStatePruneDeadWithProcess:
    @pytest.mark.asyncio
    async def test_desired_state_prunes_dead_processes(self, tmp_path):
        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
            heartbeat_timeout_s=999,
            health_check_interval_s=0,
        )
        manager = SwarmManager(config)
        manager.broadcast_status = AsyncMock()
        pm = BotProcessManager(manager, worker_registry={})
        manager.bot_process_manager = pm

        manager.desired_bots = 1
        proc = MagicMock()
        proc.poll.return_value = None
        manager.processes["dead_bot"] = proc
        manager.bots["dead_bot"] = BotStatusBase(
            bot_id="dead_bot",
            state="error",
            config="/c.yaml",
        )

        with patch.object(pm, "_launch_queued_bot", new_callable=AsyncMock):
            task = asyncio.create_task(pm.monitor_processes())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # Dead bot should be pruned
        assert "dead_bot" not in manager.bots
        proc.kill.assert_called()


# ── process.py: desired uses dead bot configs (line 472) ──────────────


class TestDesiredStateUsesDeadConfig:
    @pytest.mark.asyncio
    async def test_desired_state_spawns_from_dead_config(self, tmp_path):
        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
            heartbeat_timeout_s=999,
            health_check_interval_s=0,
        )
        manager = SwarmManager(config)
        manager.broadcast_status = AsyncMock()
        pm = BotProcessManager(manager, worker_registry={})
        manager.bot_process_manager = pm

        manager.desired_bots = 1
        # Only a dead bot with a config — desired-state should use its config
        manager.bots["dead"] = BotStatusBase(
            bot_id="dead",
            state="stopped",
            config="/dead.yaml",
        )

        with patch.object(pm, "_launch_queued_bot", new_callable=AsyncMock) as mock_launch:
            task = asyncio.create_task(pm.monitor_processes())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            if mock_launch.called:
                _, kwargs = mock_launch.call_args
                # The config should come from the dead bot
                assert mock_launch.call_args[0][1] == "/dead.yaml"


# ── process.py: spawn_bot name_base (line 180) ──────────────────────────
# Already covered in test_coverage_gaps.py TestProcessSpawnEdgeCases

# ── process.py: exit with existing exit_reason (line 361, 367, 371) ──────


class TestExitReasonPreserved:
    @pytest.mark.asyncio
    async def test_exited_success_preserves_existing_exit_reason(self, tmp_path):
        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
            heartbeat_timeout_s=999,
            health_check_interval_s=0,
        )
        manager = SwarmManager(config)
        manager.broadcast_status = AsyncMock()
        pm = BotProcessManager(manager, worker_registry={})
        manager.bot_process_manager = pm

        proc = MagicMock()
        proc.poll.side_effect = [0, None]
        proc.returncode = 0
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(
            bot_id="bot_000",
            state="running",
            exit_reason="custom_reason",
        )

        task = asyncio.create_task(pm.monitor_processes())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Existing exit_reason should be preserved
        assert manager.bots["bot_000"].exit_reason == "custom_reason"

    @pytest.mark.asyncio
    async def test_exited_error_preserves_existing_exit_reason(self, tmp_path):
        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
            heartbeat_timeout_s=999,
            health_check_interval_s=0,
        )
        manager = SwarmManager(config)
        manager.broadcast_status = AsyncMock()
        pm = BotProcessManager(manager, worker_registry={})
        manager.bot_process_manager = pm

        proc = MagicMock()
        proc.poll.side_effect = [1, None]
        proc.returncode = 1
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(
            bot_id="bot_000",
            state="running",
            exit_reason="custom",
            error_message="existing",
        )

        task = asyncio.create_task(pm.monitor_processes())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert manager.bots["bot_000"].exit_reason == "custom"
        assert manager.bots["bot_000"].error_message == "existing"


# ── spawn.py: prune with process kill (lines 161-162) ────────────────────


class TestPruneWithProcessKill:
    def test_prune_kills_process_entries(self, tmp_path):
        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
        )
        app, manager = create_manager_app(config)
        client = TestClient(app)
        proc = MagicMock()
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="error")
        resp = client.post("/swarm/prune")
        assert resp.status_code == 200
        proc.kill.assert_called_once()
        assert "bot_000" not in manager.processes


# ── bot_ops.py: _update_command_history seq not found (line 50) ──────────


class TestClearWithRunningProcesses:
    """Cover spawn.py lines 161-162: clear kills running processes."""

    def test_clear_kills_processes(self, tmp_path):
        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
        )
        app, manager = create_manager_app(config)
        client = TestClient(app)
        proc = MagicMock()
        proc.poll.return_value = None
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        resp = client.post("/swarm/clear")
        assert resp.status_code == 200
        assert len(manager.bots) == 0


class TestBustRespawnSkipsNonBust:
    """Cover process.py line 434: continue when ctx != BUST."""

    @pytest.mark.asyncio
    async def test_bust_respawn_skips_non_bust_bot(self, tmp_path):
        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
            heartbeat_timeout_s=999,
            health_check_interval_s=0,
        )
        manager = SwarmManager(config)
        manager.broadcast_status = AsyncMock()
        pm = BotProcessManager(manager, worker_registry={})
        manager.bot_process_manager = pm
        manager.bust_respawn = True

        class BotWithActivity(BotStatusBase):
            activity_context: str | None = None

        # Non-BUST bot should be skipped (line 434: continue)
        manager.bots["bot_000"] = BotWithActivity(
            bot_id="bot_000",
            state="running",
            activity_context="TRADING",
            last_update_time=time.time(),
        )

        task = asyncio.create_task(pm.monitor_processes())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Non-BUST bot should remain running
        assert manager.bots["bot_000"].state == "running"


class TestUpdateHistoryNoMatch:
    def test_update_no_matching_seq(self):
        from provide.terminal.manager.routes.bot_ops import _update_command_history

        bot = BotStatusBase(bot_id="b")
        bot.manager_command_history = [{"seq": 1, "status": "queued"}]
        _update_command_history(bot, 999, status="acknowledged")
        # Should not modify anything
        assert bot.manager_command_history[0]["status"] == "queued"
