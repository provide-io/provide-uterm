#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Integration tests for provide.uterm.manager routes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from provide.uterm.manager.app import create_manager_app
from provide.uterm.manager.config import ManagerConfig
from provide.uterm.manager.models import AgentStatusBase
from provide.uterm.manager.routes.spawn import _respawn_after_restart_exit


@pytest.fixture
def config(tmp_path):
    return ManagerConfig(
        state_file=str(tmp_path / "state.json"),
        timeseries_dir=str(tmp_path / "metrics"),
        log_dir=str(tmp_path / "logs"),
    )


@pytest.fixture
def app_and_manager(config):
    app, manager = create_manager_app(config)
    return app, manager


@pytest.fixture
def client(app_and_manager):
    app, _mgr = app_and_manager
    return TestClient(app)


@pytest.fixture
def manager(app_and_manager):
    _, mgr = app_and_manager
    return mgr


class TestAgentRestart:
    def test_restart_not_found(self, client):
        resp = client.post("/agent/nonexistent/restart")
        assert resp.status_code == 404

    def test_restart_without_plugin(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        resp = client.post("/agent/agent_000/restart")
        assert resp.status_code == 200

    def test_restart_without_plugin_schedules_respawn_for_configured_agent(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(
            agent_id="agent_000",
            state="running",
            config="/tmp/agent.yaml",
        )
        fake_task = MagicMock()

        def fake_create_task(coro):
            coro.close()
            return fake_task

        with patch(
            "provide.uterm.manager.routes.spawn.asyncio.create_task", side_effect=fake_create_task
        ) as create_task:
            resp = client.post("/agent/agent_000/restart")

        assert resp.status_code == 200
        assert fake_task in manager._background_tasks
        fake_task.add_done_callback.assert_called_once()
        create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_respawn_after_restart_exit_respawns_and_clears_pending_command(self, manager):
        agent = AgentStatusBase(agent_id="agent_000", state="stopped", config="/tmp/agent.yaml")
        agent.pending_command_seq = 7
        agent.pending_command_type = "restart"
        agent.pending_command_payload = {"reason": "test"}
        manager.agents["agent_000"] = agent
        manager.spawn_agent = AsyncMock()

        await _respawn_after_restart_exit(
            manager,
            "agent_000",
            "/tmp/agent.yaml",
            exit_timeout_s=0.1,
            poll_interval_s=0,
        )

        assert agent.pending_command_seq == 0
        assert agent.pending_command_type is None
        assert agent.pending_command_payload == {}
        manager.spawn_agent.assert_awaited_once_with("/tmp/agent.yaml", "agent_000")

    @pytest.mark.asyncio
    async def test_respawn_after_restart_exit_returns_when_agent_disappears(self, manager):
        manager.spawn_agent = AsyncMock()

        await _respawn_after_restart_exit(
            manager,
            "agent_000",
            "/tmp/agent.yaml",
            exit_timeout_s=0.1,
            poll_interval_s=0,
        )

        manager.spawn_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_respawn_after_restart_exit_times_out_while_agent_running(self, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running", config="/tmp/agent.yaml")
        manager.spawn_agent = AsyncMock()

        await _respawn_after_restart_exit(
            manager,
            "agent_000",
            "/tmp/agent.yaml",
            exit_timeout_s=0,
            poll_interval_s=0,
        )

        manager.spawn_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_respawn_after_restart_exit_swallows_spawn_failure(self, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="stopped", config="/tmp/agent.yaml")
        manager.spawn_agent = AsyncMock(side_effect=RuntimeError("spawn failed"))

        await _respawn_after_restart_exit(
            manager,
            "agent_000",
            "/tmp/agent.yaml",
            exit_timeout_s=0.1,
            poll_interval_s=0,
        )

        manager.spawn_agent.assert_awaited_once_with("/tmp/agent.yaml", "agent_000")

    @pytest.mark.asyncio
    async def test_respawn_after_restart_exit_polls_until_agent_stops(self, manager):
        agent = AgentStatusBase(agent_id="agent_000", state="running", config="/tmp/agent.yaml")
        manager.agents["agent_000"] = agent
        manager.spawn_agent = AsyncMock()
        polls = 0

        async def stop_after_first_poll(_interval):
            nonlocal polls
            polls += 1
            if polls == 2:
                agent.state = "stopped"

        with patch("provide.uterm.manager.routes.spawn.asyncio.sleep", side_effect=stop_after_first_poll):
            await _respawn_after_restart_exit(
                manager,
                "agent_000",
                "/tmp/agent.yaml",
                exit_timeout_s=0.1,
                poll_interval_s=0,
            )

        manager.spawn_agent.assert_awaited_once_with("/tmp/agent.yaml", "agent_000")
        assert polls == 2

    @pytest.mark.asyncio
    async def test_respawn_after_restart_exit_handles_agent_removed_after_terminal_state(self, manager):
        agent = AgentStatusBase(agent_id="agent_000", state="stopped", config="/tmp/agent.yaml")

        class VanishingAgents(dict):
            def __init__(self):
                super().__init__({"agent_000": agent})
                self.get_calls = 0

            def get(self, key, default=None):
                self.get_calls += 1
                if self.get_calls == 1:
                    return super().get(key, default)
                return default

        manager.agents = VanishingAgents()
        manager.spawn_agent = AsyncMock()

        await _respawn_after_restart_exit(
            manager,
            "agent_000",
            "/tmp/agent.yaml",
            exit_timeout_s=0.1,
            poll_interval_s=0,
        )

        manager.spawn_agent.assert_awaited_once_with("/tmp/agent.yaml", "agent_000")


class TestSetGoal:
    def test_not_found(self, client):
        resp = client.post("/agent/nonexistent/set-goal?goal=trade")
        assert resp.status_code == 404

    def test_without_plugin(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        resp = client.post("/agent/agent_000/set-goal?goal=trade")
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "set_goal"


class TestSetDirective:
    def test_not_found(self, client):
        resp = client.post("/agent/nonexistent/set-directive", json={"directive": "go"})
        assert resp.status_code == 404

    def test_without_plugin(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        resp = client.post("/agent/agent_000/set-directive", json={"directive": "test", "turns": 5})
        assert resp.status_code == 200


class TestCancelCommand:
    def test_not_found(self, client):
        resp = client.post("/agent/nonexistent/cancel-command")
        assert resp.status_code == 404

    def test_no_pending(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        resp = client.post("/agent/agent_000/cancel-command")
        assert resp.status_code == 200
        assert resp.json()["result"]["cancelled"] is False

    def test_cancel_pending(self, client, manager):
        agent = AgentStatusBase(agent_id="agent_000", state="running")
        agent.pending_command_seq = 1
        agent.pending_command_type = "pause"
        manager.agents["agent_000"] = agent
        resp = client.post("/agent/agent_000/cancel-command")
        assert resp.status_code == 200
        assert resp.json()["result"]["cancelled"] is True
        assert manager.agents["agent_000"].pending_command_seq == 0


class TestKillAgent:
    def test_not_found(self, client):
        resp = client.delete("/agent/nonexistent")
        assert resp.status_code == 404

    def test_remove_terminal_agent(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="stopped")
        resp = client.delete("/agent/agent_000")
        assert resp.status_code == 200
        assert "agent_000" not in manager.agents

    def test_remove_no_process(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        # No process entry
        resp = client.delete("/agent/agent_000")
        assert resp.status_code == 200
        assert "agent_000" not in manager.agents


class TestAgentEvents:
    def test_not_found(self, client):
        resp = client.get("/agent/nonexistent/events")
        assert resp.status_code == 404

    def test_empty_events(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running", last_update_time=1.0)
        resp = client.get("/agent/agent_000/events")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "agent_000"
        assert len(data["events"]) >= 1  # at least status_update


class TestWebSocket:
    def test_websocket_swarm(self, client, manager):
        with client.websocket_connect("/ws/swarm") as ws:
            # Send a message (the handler just receives text)
            ws.send_text("ping")
            # The server should have added us to websocket_clients
            # We can't easily verify async state from sync test, but
            # at least verify the connection works.
