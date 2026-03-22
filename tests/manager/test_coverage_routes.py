# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Coverage tests for manager routes — bot_ops, bot_update, spawn, and branch arcs."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from provide.terminal.manager.app import create_manager_app
from provide.terminal.manager.config import ManagerConfig
from provide.terminal.manager.models import BotStatusBase
from provide.terminal.manager.routes.bot_ops import (
    _append_command_history,
    _command_history_rows,
    _queue_manager_command,
    _update_command_history,
)


class FakeWorkerPlugin:
    @property
    def worker_type(self) -> str:
        return "test_game"

    @property
    def worker_module(self) -> str:
        return "test_module"

    def configure_worker_env(self, env, bot_status, manager, **kwargs):
        pass


def _make_plugin_no_local_bot() -> MagicMock:
    """Plugin mock that reports no local bot (triggers queue fallback path)."""
    plugin = MagicMock()
    plugin.resolve_local_bot.return_value = (None, None)
    plugin.build_action_response.return_value = {
        "bot_id": "bot_000",
        "action": "test",
        "source": "worker_queue",
        "applied": False,
        "queued": True,
        "result": {},
        "state": "running",
    }
    return plugin


class TestCommandHistoryEdgeCases:
    """Cover bot_ops.py lines 33-34, 42, 47, 49-52, 58."""

    def test_command_history_rows_creates_list(self):
        bot = BotStatusBase(bot_id="b")
        bot.manager_command_history = None  # type: ignore[assignment]
        rows = _command_history_rows(bot)
        assert rows == []
        assert bot.manager_command_history == []

    def test_append_trims_to_25(self):
        bot = BotStatusBase(bot_id="b")
        for i in range(30):
            _append_command_history(bot, {"seq": i})
        assert len(bot.manager_command_history) == 25

    def test_update_history_no_match(self):
        bot = BotStatusBase(bot_id="b")
        _update_command_history(bot, 0)

    def test_update_history_finds_and_patches(self):
        bot = BotStatusBase(bot_id="b")
        _append_command_history(bot, {"seq": 5, "status": "queued"})
        _update_command_history(bot, 5, status="acknowledged")
        assert bot.manager_command_history[-1]["status"] == "acknowledged"

    def test_update_history_skip_non_matching(self):
        bot = BotStatusBase(bot_id="b")
        _append_command_history(bot, {"seq": 1, "status": "queued"})
        _append_command_history(bot, {"seq": 5, "status": "queued"})
        _update_command_history(bot, 5, status="done")
        assert bot.manager_command_history[0]["status"] == "queued"
        assert bot.manager_command_history[1]["status"] == "done"

    def test_queue_replaces_existing(self):
        bot = BotStatusBase(bot_id="b")
        _queue_manager_command(bot, "pause", {})
        _queue_manager_command(bot, "resume", {})
        assert bot.pending_command_type == "resume"
        assert bot.pending_command_seq == 2


class TestBotOpsDescribeRuntime:
    """Cover bot_ops.py line 176: local_runtime in status response."""

    def test_status_with_runtime(self, tmp_path):
        plugin = MagicMock()
        plugin.resolve_local_bot.return_value = (MagicMock(), "sess_1")
        plugin.describe_runtime.return_value = {"available": True, "bot_type": "TW"}
        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
        )
        app, manager = create_manager_app(config, managed_bot=plugin)
        client = TestClient(app)
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        resp = client.get("/bot/bot_000/status")
        assert resp.status_code == 200
        assert resp.json()["local_runtime"]["available"] is True


class TestBotUpdateAllFields:
    """Cover bot_update.py lines 82-83, 88, 96, 98, 100, 102, 106, 108, 112."""

    @pytest.fixture
    def client_and_manager(self, tmp_path):
        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
        )
        app, mgr = create_manager_app(config)
        return TestClient(app), mgr

    def test_update_all_base_fields(self, client_and_manager):
        client, manager = client_and_manager
        manager.bots["b"] = BotStatusBase(bot_id="b", state="running")
        resp = client.post(
            "/bot/b/status",
            json={
                "reported_at": 1000.0,
                "started_at": 500.0,
                "stopped_at": 600.0,
                "last_action": "TRADE",
                "last_action_time": 550.0,
                "error_type": "TestErr",
                "error_timestamp": 580.0,
                "recent_actions": [{"action": "MOVE"}],
            },
        )
        assert resp.status_code == 200
        bot = manager.bots["b"]
        assert bot.status_reported_at == 1000.0
        assert bot.started_at == 500.0
        assert bot.stopped_at == 600.0
        assert bot.last_action == "TRADE"
        assert bot.last_action_time == 550.0
        assert bot.error_type == "TestErr"
        assert bot.error_timestamp == 580.0
        assert bot.recent_actions == [{"action": "MOVE"}]

    def test_reported_at_none(self, client_and_manager):
        client, manager = client_and_manager
        manager.bots["b"] = BotStatusBase(bot_id="b", state="running")
        resp = client.post("/bot/b/status", json={"reported_at": None})
        assert resp.status_code == 200
        assert manager.bots["b"].status_reported_at is None


class TestRoutesModels:
    """Cover models.py: get_account_pool."""

    def test_get_account_pool(self, tmp_path):
        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
        )
        pool = MagicMock()
        app, manager = create_manager_app(config, account_pool=pool)
        assert manager.account_pool is pool


class TestSpawnRouteCoverage:
    """Cover spawn.py lines 83-91, 152-153, 161-162, 181-183."""

    @pytest.fixture
    def setup(self, tmp_path):
        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
            log_dir=str(tmp_path / "logs"),
        )
        app, manager = create_manager_app(
            config,
            worker_registry={"test_game": FakeWorkerPlugin()},
        )
        return TestClient(app), manager, tmp_path

    def test_spawn_success(self, setup):
        client, manager, tmp_path = setup
        config = tmp_path / "test.yaml"
        config.write_text("worker_type: test_game\n")
        mock_proc = MagicMock(pid=123)
        manager.broadcast_status = AsyncMock()
        with patch.object(manager.bot_process_manager, "_spawn_process", return_value=mock_proc):
            resp = client.post(f"/swarm/spawn?config_path={config}")
        assert resp.status_code == 200
        assert "bot_id" in resp.json()

    def test_spawn_with_explicit_id(self, setup):
        client, manager, tmp_path = setup
        config = tmp_path / "test.yaml"
        config.write_text("worker_type: test_game\n")
        mock_proc = MagicMock(pid=123)
        manager.broadcast_status = AsyncMock()
        with patch.object(manager.bot_process_manager, "_spawn_process", return_value=mock_proc):
            resp = client.post(f"/swarm/spawn?config_path={config}&bot_id=bot_099")
        assert resp.status_code == 200
        assert resp.json()["bot_id"] == "bot_099"

    def test_spawn_failure(self, setup):
        client, manager, tmp_path = setup
        config = tmp_path / "test.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()
        with patch.object(manager.bot_process_manager, "_spawn_process", side_effect=OSError("fail")):
            resp = client.post(f"/swarm/spawn?config_path={config}")
        assert resp.status_code == 400

    def test_kill_all_with_failure(self, setup):
        client, manager, _ = setup
        proc = MagicMock()
        proc.poll.return_value = None
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.kill_bot = AsyncMock(side_effect=RuntimeError("fail"))
        resp = client.post("/swarm/kill-all")
        assert resp.status_code == 200

    def test_prune_with_processes(self, setup):
        client, manager, _ = setup
        proc = MagicMock()
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="stopped")
        manager.kill_bot = AsyncMock()
        resp = client.post("/swarm/prune")
        assert resp.status_code == 200
        assert "bot_000" not in manager.bots
        assert "bot_000" not in manager.processes
        manager.kill_bot.assert_awaited_once_with("bot_000")


class TestBotOpsPluginLocalBotNone:
    """bot_ops.py arcs 234->248, 276->292, 375->377: plugin has no local bot."""

    @pytest.fixture
    def setup_with_plugin(self, tmp_path):
        plugin = _make_plugin_no_local_bot()
        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
        )
        app, manager = create_manager_app(config, managed_bot=plugin)
        return TestClient(app), manager

    def test_set_goal_queued_when_local_bot_none(self, setup_with_plugin):
        """arc 234->248: local_bot is None → falls through to queue."""
        client, manager = setup_with_plugin
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        resp = client.post("/bot/bot_000/set-goal?goal=test+goal")
        assert resp.status_code == 200

    def test_set_directive_queued_when_local_bot_none(self, setup_with_plugin):
        """arc 276->292: local_bot is None → falls through to queue."""
        client, manager = setup_with_plugin
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        resp = client.post("/bot/bot_000/set-directive", json={"directive": "do this", "turns": 5})
        assert resp.status_code == 200

    def test_remove_bot_desired_zero(self, setup_with_plugin):
        """arc 375->377: desired_bots == 0 → skip decrement."""
        client, manager = setup_with_plugin
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="stopped")
        manager.desired_bots = 0
        with patch.object(manager, "kill_bot", new_callable=AsyncMock):
            resp = client.delete("/bot/bot_000")
        assert resp.status_code == 200
        assert manager.desired_bots == 0


class TestSpawnPauseResumeBotLocalNone:
    """spawn.py arcs 192->191, 203->202, 220->235, 258->273, 295->312."""

    @pytest.fixture
    def setup(self, tmp_path):
        plugin = _make_plugin_no_local_bot()
        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
        )
        app, manager = create_manager_app(config, managed_bot=plugin)
        return TestClient(app), manager

    def test_swarm_pause_bot_not_in_running_states(self, setup):
        """arc 192->191: bot.state not in running/recovering/blocked → if False."""
        client, manager = setup
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="stopped")
        resp = client.post("/swarm/pause")
        assert resp.status_code == 200
        assert manager.bots["bot_000"].paused is False

    def test_swarm_resume_bot_not_paused(self, setup):
        """arc 203->202: bot.paused is False → loop continues."""
        client, manager = setup
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running", paused=False)
        resp = client.post("/swarm/resume")
        assert resp.status_code == 200
        assert resp.json()["resumed"] == 0

    def test_pause_bot_local_none_queues(self, setup):
        """arc 220->235: local_bot is None → queue command."""
        client, manager = setup
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        resp = client.post("/bot/bot_000/pause", json={})
        assert resp.status_code == 200

    def test_resume_bot_local_none_queues(self, setup):
        """arc 258->273: local_bot is None → queue command."""
        client, manager = setup
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        resp = client.post("/bot/bot_000/resume", json={})
        assert resp.status_code == 200

    def test_restart_bot_local_none_queues(self, setup):
        """arc 295->312: local_bot is None → queue command."""
        client, manager = setup
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        resp = client.post("/bot/bot_000/restart", json={})
        assert resp.status_code == 200
