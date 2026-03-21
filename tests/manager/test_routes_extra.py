# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Additional route tests — plugin paths, config validation, edge cases."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from provide.terminal.manager.app import create_manager_app
from provide.terminal.manager.config import ManagerConfig
from provide.terminal.manager.models import BotStatusBase
from provide.terminal.manager.routes.spawn import _validate_config_path


@pytest.fixture
def config(tmp_path):
    return ManagerConfig(
        state_file=str(tmp_path / "state.json"),
        timeseries_dir=str(tmp_path / "metrics"),
        log_dir=str(tmp_path / "logs"),
    )


@pytest.fixture
def app_and_manager(config):
    return create_manager_app(config)


@pytest.fixture
def client(app_and_manager):
    app, _ = app_and_manager
    return TestClient(app)


@pytest.fixture
def manager(app_and_manager):
    _, mgr = app_and_manager
    return mgr


class TestConfigValidation:
    def test_valid_yaml(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("key: value")
        result = _validate_config_path(str(f))
        assert result == f.resolve()

    def test_valid_yml(self, tmp_path):
        f = tmp_path / "config.yml"
        f.write_text("key: value")
        result = _validate_config_path(str(f))
        assert result == f.resolve()

    def test_non_yaml_rejected(self, tmp_path):
        f = tmp_path / "config.json"
        f.write_text("{}")
        with pytest.raises(ValueError, match="must be a .yaml"):
            _validate_config_path(str(f))

    def test_outside_config_dir(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("key: value")
        with (
            patch.dict(os.environ, {"UTERM_CONFIG_DIR": "/some/other/dir"}),
            pytest.raises(ValueError, match="outside config dir"),
        ):
            _validate_config_path(str(f))

    def test_within_config_dir(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("key: value")
        with patch.dict(os.environ, {"UTERM_CONFIG_DIR": str(tmp_path)}):
            result = _validate_config_path(str(f))
            assert result == f.resolve()


class TestSpawnRoute:
    def test_spawn_invalid_config(self, client):
        resp = client.post("/swarm/spawn?config_path=bad.json")
        assert resp.status_code == 400

    def test_spawn_batch(self, client, manager, tmp_path):
        config = tmp_path / "c.yaml"
        config.write_text("worker_type: test\n")
        manager.start_spawn_swarm = AsyncMock()
        resp = client.post(
            "/swarm/spawn-batch",
            json={
                "config_paths": [str(config)],
                "group_size": 1,
                "group_delay": 0.1,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_bots"] == 1
        assert manager.desired_bots == 1


class TestKillBotWithProcess:
    def test_kill_running_bot_with_process(self, client, manager):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        manager.processes["bot_000"] = mock_proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.desired_bots = 2
        manager.kill_bot = AsyncMock()
        resp = client.delete("/bot/bot_000")
        assert resp.status_code == 200
        assert manager.desired_bots == 1  # decremented


class TestPluginIntegration:
    def test_bot_status_with_plugin(self, config):
        plugin = MagicMock()
        plugin.resolve_local_bot.return_value = (None, None)
        plugin.describe_runtime.return_value = None
        app, manager = create_manager_app(config, managed_bot=plugin)
        client = TestClient(app)
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        resp = client.get("/bot/bot_000/status")
        assert resp.status_code == 200

    def test_bot_details_with_plugin(self, config):
        plugin = MagicMock()
        plugin.resolve_local_bot.return_value = (MagicMock(), "sess_1")
        plugin.build_details.return_value = {"custom": True}
        app, manager = create_manager_app(config, managed_bot=plugin)
        client = TestClient(app)
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        resp = client.get("/bot/bot_000/details")
        assert resp.status_code == 200
        assert resp.json()["custom"] is True

    def test_set_goal_with_local_bot(self, config):
        plugin = MagicMock()
        plugin.resolve_local_bot.return_value = (MagicMock(), "sess_1")
        plugin.dispatch_command = AsyncMock(return_value={"ok": True})
        plugin.build_action_response.return_value = {"action": "set_goal", "applied": True}
        app, manager = create_manager_app(config, managed_bot=plugin)
        client = TestClient(app)
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        resp = client.post("/bot/bot_000/set-goal?goal=trade")
        assert resp.status_code == 200

    def test_set_goal_with_local_error(self, config):
        plugin = MagicMock()
        plugin.resolve_local_bot.return_value = (MagicMock(), "sess_1")
        plugin.dispatch_command = AsyncMock(return_value={"error": "not supported"})
        app, manager = create_manager_app(config, managed_bot=plugin)
        client = TestClient(app)
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        resp = client.post("/bot/bot_000/set-goal?goal=trade")
        assert resp.status_code == 400

    def test_set_directive_with_local_bot(self, config):
        plugin = MagicMock()
        plugin.resolve_local_bot.return_value = (MagicMock(), "sess_1")
        plugin.dispatch_command = AsyncMock(return_value={"directive": "go", "turns": 5})
        plugin.build_action_response.return_value = {"action": "set_directive", "applied": True}
        app, manager = create_manager_app(config, managed_bot=plugin)
        client = TestClient(app)
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        resp = client.post("/bot/bot_000/set-directive", json={"directive": "go", "turns": 5})
        assert resp.status_code == 200

    def test_set_directive_with_local_error(self, config):
        plugin = MagicMock()
        plugin.resolve_local_bot.return_value = (MagicMock(), "sess_1")
        plugin.dispatch_command = AsyncMock(return_value={"error": "nope"})
        app, manager = create_manager_app(config, managed_bot=plugin)
        client = TestClient(app)
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        resp = client.post("/bot/bot_000/set-directive", json={"directive": "go"})
        assert resp.status_code == 400

    def test_pause_with_local_bot(self, config):
        plugin = MagicMock()
        plugin.resolve_local_bot.return_value = (MagicMock(), "sess_1")
        plugin.dispatch_command = AsyncMock(return_value={"paused": True})
        plugin.build_action_response.return_value = {"action": "pause", "applied": True}
        app, manager = create_manager_app(config, managed_bot=plugin)
        client = TestClient(app)
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        resp = client.post("/bot/bot_000/pause")
        assert resp.status_code == 200

    def test_pause_with_local_error(self, config):
        plugin = MagicMock()
        plugin.resolve_local_bot.return_value = (MagicMock(), "sess_1")
        plugin.dispatch_command = AsyncMock(return_value={"error": "nope"})
        app, manager = create_manager_app(config, managed_bot=plugin)
        client = TestClient(app)
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        resp = client.post("/bot/bot_000/pause")
        assert resp.status_code == 400

    def test_resume_with_local_bot(self, config):
        plugin = MagicMock()
        plugin.resolve_local_bot.return_value = (MagicMock(), "sess_1")
        plugin.dispatch_command = AsyncMock(return_value={"paused": False})
        plugin.build_action_response.return_value = {"action": "resume", "applied": True}
        app, manager = create_manager_app(config, managed_bot=plugin)
        client = TestClient(app)
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running", paused=True)
        resp = client.post("/bot/bot_000/resume")
        assert resp.status_code == 200

    def test_resume_with_local_error(self, config):
        plugin = MagicMock()
        plugin.resolve_local_bot.return_value = (MagicMock(), "sess_1")
        plugin.dispatch_command = AsyncMock(return_value={"error": "nope"})
        app, manager = create_manager_app(config, managed_bot=plugin)
        client = TestClient(app)
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running", paused=True)
        resp = client.post("/bot/bot_000/resume")
        assert resp.status_code == 400

    def test_restart_with_local_bot(self, config):
        plugin = MagicMock()
        plugin.resolve_local_bot.return_value = (MagicMock(), "sess_1")
        plugin.dispatch_command = AsyncMock(return_value={"restarted": True})
        plugin.build_action_response.return_value = {"action": "restart", "applied": True}
        app, manager = create_manager_app(config, managed_bot=plugin)
        client = TestClient(app)
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        resp = client.post("/bot/bot_000/restart")
        assert resp.status_code == 200

    def test_restart_with_local_error(self, config):
        plugin = MagicMock()
        plugin.resolve_local_bot.return_value = (MagicMock(), "sess_1")
        plugin.dispatch_command = AsyncMock(return_value={"error": "fail"})
        app, manager = create_manager_app(config, managed_bot=plugin)
        client = TestClient(app)
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        resp = client.post("/bot/bot_000/restart")
        assert resp.status_code == 400


class TestStatusUpdateWithPlugin:
    def test_plugin_apply_update_called(self, config):
        plugin = MagicMock()
        app, manager = create_manager_app(config, status_update=plugin)
        client = TestClient(app)
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        resp = client.post("/bot/bot_000/status", json={"state": "recovering"})
        assert resp.status_code == 200
        plugin.apply_update.assert_called_once()


class TestSessionDataWithPlugin:
    def test_identity_store_with_model_dump(self, config):
        store = MagicMock()
        record = MagicMock()
        record.model_dump.return_value = {"bot_id": "bot_000", "sessions": []}
        store.load.return_value = record
        app, manager = create_manager_app(config, identity_store=store)
        client = TestClient(app)
        resp = client.get("/bot/bot_000/session-data")
        assert resp.status_code == 200
        assert resp.json()["bot_id"] == "bot_000"

    def test_identity_store_with_dict(self, config):
        store = MagicMock()
        # Return a plain dict (no model_dump method)
        plain_dict = {"bot_id": "bot_000"}
        store.load.return_value = plain_dict
        app, manager = create_manager_app(config, identity_store=store)
        client = TestClient(app)
        resp = client.get("/bot/bot_000/session-data")
        assert resp.status_code == 200


class TestBotEventsWithActions:
    def test_events_with_recent_actions(self, client, manager):
        bot = BotStatusBase(
            bot_id="bot_000",
            state="running",
            last_update_time=100.0,
            recent_actions=[
                {"time": 50.0, "action": "TRADE", "sector": 5},
                {"time": 60.0, "action": "MOVE", "sector": 10},
            ],
            error_timestamp=70.0,
            error_type="TestError",
            error_message="test error",
        )
        manager.bots["bot_000"] = bot
        resp = client.get("/bot/bot_000/events")
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert len(events) == 3  # 2 actions + 1 error
        assert events[0]["type"] == "error"  # most recent first
