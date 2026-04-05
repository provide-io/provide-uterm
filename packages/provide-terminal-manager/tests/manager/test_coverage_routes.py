#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Coverage tests for manager routes — agent_ops, agent_update, spawn, and branch arcs."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from provide.terminal.manager.app import create_manager_app
from provide.terminal.manager.config import ManagerConfig
from provide.terminal.manager.models import AgentStatusBase
from provide.terminal.manager.routes.agent_ops import (
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

    def configure_worker_env(self, env, agent_status, manager, **kwargs):
        pass


def _make_plugin_no_local_agent() -> MagicMock:
    """Plugin mock that reports no local agent (triggers queue fallback path)."""
    plugin = MagicMock()
    plugin.resolve_local_agent.return_value = (None, None)
    plugin.build_action_response.return_value = {
        "agent_id": "agent_000",
        "action": "test",
        "source": "worker_queue",
        "applied": False,
        "queued": True,
        "result": {},
        "state": "running",
    }
    return plugin


class TestCommandHistoryEdgeCases:
    """Cover agent_ops.py lines 33-34, 42, 47, 49-52, 58."""

    def test_command_history_rows_creates_list(self):
        agent = AgentStatusBase(agent_id="b")
        agent.manager_command_history = None  # type: ignore[assignment]
        rows = _command_history_rows(agent)
        assert rows == []
        assert agent.manager_command_history == []

    def test_append_trims_to_25(self):
        agent = AgentStatusBase(agent_id="b")
        for i in range(30):
            _append_command_history(agent, {"seq": i})
        assert len(agent.manager_command_history) == 25

    def test_update_history_no_match(self):
        agent = AgentStatusBase(agent_id="b")
        _update_command_history(agent, 0)

    def test_update_history_finds_and_patches(self):
        agent = AgentStatusBase(agent_id="b")
        _append_command_history(agent, {"seq": 5, "status": "queued"})
        _update_command_history(agent, 5, status="acknowledged")
        assert agent.manager_command_history[-1]["status"] == "acknowledged"

    def test_update_history_skip_non_matching(self):
        agent = AgentStatusBase(agent_id="b")
        _append_command_history(agent, {"seq": 1, "status": "queued"})
        _append_command_history(agent, {"seq": 5, "status": "queued"})
        _update_command_history(agent, 5, status="done")
        assert agent.manager_command_history[0]["status"] == "queued"
        assert agent.manager_command_history[1]["status"] == "done"

    def test_queue_replaces_existing(self):
        agent = AgentStatusBase(agent_id="b")
        _queue_manager_command(agent, "pause", {})
        _queue_manager_command(agent, "resume", {})
        assert agent.pending_command_type == "resume"
        assert agent.pending_command_seq == 2


class TestAgentOpsDescribeRuntime:
    """Cover agent_ops.py line 176: local_runtime in status response."""

    def test_status_with_runtime(self, tmp_path):
        plugin = MagicMock()
        plugin.resolve_local_agent.return_value = (MagicMock(), "sess_1")
        plugin.describe_runtime.return_value = {"available": True, "agent_type": "TW"}
        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
        )
        app, manager = create_manager_app(config, managed_agent=plugin)
        client = TestClient(app)
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        resp = client.get("/agent/agent_000/status")
        assert resp.status_code == 200
        assert resp.json()["local_runtime"]["available"] is True


class TestAgentUpdateAllFields:
    """Cover agent_update.py lines 82-83, 88, 96, 98, 100, 102, 106, 108, 112."""

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
        manager.agents["b"] = AgentStatusBase(agent_id="b", state="running")
        resp = client.post(
            "/agent/b/status",
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
        agent = manager.agents["b"]
        assert agent.status_reported_at == 1000.0
        assert agent.started_at == 500.0
        assert agent.stopped_at == 600.0
        assert agent.last_action == "TRADE"
        assert agent.last_action_time == 550.0
        assert agent.error_type == "TestErr"
        assert agent.error_timestamp == 580.0
        assert agent.recent_actions == [{"action": "MOVE"}]

    def test_reported_at_none(self, client_and_manager):
        client, manager = client_and_manager
        manager.agents["b"] = AgentStatusBase(agent_id="b", state="running")
        resp = client.post("/agent/b/status", json={"reported_at": None})
        assert resp.status_code == 200
        assert manager.agents["b"].status_reported_at is None


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
        with patch.object(manager.agent_process_manager, "_spawn_process", return_value=mock_proc):
            resp = client.post(f"/swarm/spawn?config_path={config}")
        assert resp.status_code == 200
        assert "agent_id" in resp.json()

    def test_spawn_with_explicit_id(self, setup):
        client, manager, tmp_path = setup
        config = tmp_path / "test.yaml"
        config.write_text("worker_type: test_game\n")
        mock_proc = MagicMock(pid=123)
        manager.broadcast_status = AsyncMock()
        with patch.object(manager.agent_process_manager, "_spawn_process", return_value=mock_proc):
            resp = client.post(f"/swarm/spawn?config_path={config}&agent_id=agent_099")
        assert resp.status_code == 200
        assert resp.json()["agent_id"] == "agent_099"

    def test_spawn_failure(self, setup):
        client, manager, tmp_path = setup
        config = tmp_path / "test.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()
        with patch.object(manager.agent_process_manager, "_spawn_process", side_effect=OSError("fail")):
            resp = client.post(f"/swarm/spawn?config_path={config}")
        assert resp.status_code == 400

    def test_kill_all_with_failure(self, setup):
        client, manager, _ = setup
        proc = MagicMock()
        proc.poll.return_value = None
        manager.processes["agent_000"] = proc
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.kill_agent = AsyncMock(side_effect=RuntimeError("fail"))
        resp = client.post("/swarm/kill-all")
        assert resp.status_code == 200

    def test_prune_with_processes(self, setup):
        client, manager, _ = setup
        proc = MagicMock()
        manager.processes["agent_000"] = proc
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="stopped")
        manager.kill_agent = AsyncMock()
        resp = client.post("/swarm/prune")
        assert resp.status_code == 200
        assert "agent_000" not in manager.agents
        assert "agent_000" not in manager.processes
        manager.kill_agent.assert_awaited_once_with("agent_000")


class TestAgentOpsPluginLocalAgentNone:
    """agent_ops.py arcs 234->248, 276->292, 375->377: plugin has no local agent."""

    @pytest.fixture
    def setup_with_plugin(self, tmp_path):
        plugin = _make_plugin_no_local_agent()
        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
        )
        app, manager = create_manager_app(config, managed_agent=plugin)
        return TestClient(app), manager

    def test_set_goal_queued_when_local_agent_none(self, setup_with_plugin):
        """arc 234->248: local_agent is None → falls through to queue."""
        client, manager = setup_with_plugin
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        resp = client.post("/agent/agent_000/set-goal?goal=test+goal")
        assert resp.status_code == 200

    def test_set_directive_queued_when_local_agent_none(self, setup_with_plugin):
        """arc 276->292: local_agent is None → falls through to queue."""
        client, manager = setup_with_plugin
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        resp = client.post("/agent/agent_000/set-directive", json={"directive": "do this", "turns": 5})
        assert resp.status_code == 200

    def test_remove_agent_desired_zero(self, setup_with_plugin):
        """arc 375->377: desired_agents == 0 → skip decrement."""
        client, manager = setup_with_plugin
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="stopped")
        manager.desired_agents = 0
        with patch.object(manager, "kill_agent", new_callable=AsyncMock):
            resp = client.delete("/agent/agent_000")
        assert resp.status_code == 200
        assert manager.desired_agents == 0


class TestSpawnPauseResumeAgentLocalNone:
    """spawn.py arcs 192->191, 203->202, 220->235, 258->273, 295->312."""

    @pytest.fixture
    def setup(self, tmp_path):
        plugin = _make_plugin_no_local_agent()
        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
        )
        app, manager = create_manager_app(config, managed_agent=plugin)
        return TestClient(app), manager

    def test_swarm_pause_agent_not_in_running_states(self, setup):
        """arc 192->191: agent.state not in running/recovering/blocked → if False."""
        client, manager = setup
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="stopped")
        resp = client.post("/swarm/pause")
        assert resp.status_code == 200
        assert manager.agents["agent_000"].paused is False

    def test_swarm_resume_agent_not_paused(self, setup):
        """arc 203->202: agent.paused is False → loop continues."""
        client, manager = setup
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running", paused=False)
        resp = client.post("/swarm/resume")
        assert resp.status_code == 200
        assert resp.json()["resumed"] == 0

    def test_pause_agent_local_none_queues(self, setup):
        """arc 220->235: local_agent is None → queue command."""
        client, manager = setup
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        resp = client.post("/agent/agent_000/pause", json={})
        assert resp.status_code == 200

    def test_resume_agent_local_none_queues(self, setup):
        """arc 258->273: local_agent is None → queue command."""
        client, manager = setup
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        resp = client.post("/agent/agent_000/resume", json={})
        assert resp.status_code == 200

    def test_restart_agent_local_none_queues(self, setup):
        """arc 295->312: local_agent is None → queue command."""
        client, manager = setup
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        resp = client.post("/agent/agent_000/restart", json={})
        assert resp.status_code == 200
