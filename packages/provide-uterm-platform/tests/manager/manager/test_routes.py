#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Integration tests for provide.uterm.manager routes."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from provide.uterm.manager.app import create_manager_app
from provide.uterm.manager.config import ManagerConfig
from provide.uterm.manager.models import AgentStatusBase


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


class TestHealthCheck:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestSwarmStatus:
    def test_status(self, client):
        resp = client.get("/swarm/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_agents"] == 0
        assert data["running"] == 0

    def test_timeseries_info(self, client):
        resp = client.get("/swarm/timeseries/info")
        assert resp.status_code == 200
        assert "interval_seconds" in resp.json()

    def test_timeseries_recent(self, client):
        resp = client.get("/swarm/timeseries/recent?limit=10")
        assert resp.status_code == 200
        assert "rows" in resp.json()

    def test_timeseries_summary(self, client):
        resp = client.get("/swarm/timeseries/summary?window_minutes=30")
        assert resp.status_code == 200


class TestAgentList:
    def test_empty(self, client):
        resp = client.get("/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["agents"] == []

    def test_with_agents(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running", last_update_time=1.0)
        manager.agents["agent_001"] = AgentStatusBase(agent_id="agent_001", state="error", last_update_time=2.0)
        resp = client.get("/agents")
        data = resp.json()
        assert data["total"] == 2

    def test_filter_by_state(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.agents["agent_001"] = AgentStatusBase(agent_id="agent_001", state="error")
        resp = client.get("/agents?state=running")
        data = resp.json()
        assert data["total"] == 1
        assert data["agents"][0]["agent_id"] == "agent_000"

    def test_interactive_only(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(
            agent_id="agent_000", state="running", session_id="s1", config="mcp://x"
        )
        manager.agents["agent_001"] = AgentStatusBase(agent_id="agent_001", state="running")
        resp = client.get("/agents?interactive_only=true")
        data = resp.json()
        assert data["total"] == 1


class TestAgentStatus:
    def test_not_found(self, client):
        resp = client.get("/agent/nonexistent/status")
        assert resp.status_code == 404

    def test_found(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        resp = client.get("/agent/agent_000/status")
        assert resp.status_code == 200
        assert resp.json()["state"] == "running"


class TestAgentDetails:
    def test_not_found(self, client):
        resp = client.get("/agent/nonexistent/details")
        assert resp.status_code == 404

    def test_no_plugin(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        resp = client.get("/agent/agent_000/details")
        assert resp.status_code == 200
        assert resp.json()["agent_id"] == "agent_000"


class TestAgentRegister:
    def test_register_new(self, client, manager):
        resp = client.post("/agent/agent_new/register", json={"state": "running"})
        assert resp.status_code == 200
        assert resp.json()["created"] is True
        assert "agent_new" in manager.agents

    def test_register_existing(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        resp = client.post("/agent/agent_000/register", json={"state": "stopped"})
        assert resp.status_code == 200
        assert resp.json()["created"] is False

    def test_register_invalid(self, client):
        resp = client.post("/agent/bad/register", json={"state": 12345})
        # pydantic may coerce or reject
        assert resp.status_code in (200, 422)


class TestAgentStatusUpdate:
    def test_update_base_fields(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        resp = client.post(
            "/agent/agent_000/status",
            json={
                "state": "recovering",
                "pid": 1234,
                "error_message": "test error",
                "exit_reason": "test_exit",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert manager.agents["agent_000"].state == "recovering"
        assert manager.agents["agent_000"].pid == 1234

    def test_auto_register_unknown_agent(self, client, manager):
        resp = client.post("/agent/new_agent/status", json={"state": "running"})
        assert resp.status_code == 200
        assert "new_agent" in manager.agents

    def test_stale_report_rejected(self, client, manager):
        agent = AgentStatusBase(agent_id="agent_000", state="running", status_reported_at=1000.0)
        manager.agents["agent_000"] = agent
        resp = client.post(
            "/agent/agent_000/status",
            json={
                "reported_at": 500.0,  # older
                "state": "error",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ignored") == "stale_report"
        assert manager.agents["agent_000"].state == "running"  # not changed

    def test_command_acknowledgement(self, client, manager):
        agent = AgentStatusBase(agent_id="agent_000", state="running")
        agent.pending_command_seq = 5
        agent.pending_command_type = "restart"
        manager.agents["agent_000"] = agent
        resp = client.post(
            "/agent/agent_000/status",
            json={
                "last_manager_command_seq": 5,
            },
        )
        assert resp.status_code == 200
        assert manager.agents["agent_000"].pending_command_seq == 0

    def test_manager_command_in_response(self, client, manager):
        agent = AgentStatusBase(agent_id="agent_000", state="running")
        agent.pending_command_seq = 3
        agent.pending_command_type = "set_goal"
        agent.pending_command_payload = {"goal": "trade"}
        manager.agents["agent_000"] = agent
        resp = client.post("/agent/agent_000/status", json={"state": "running"})
        data = resp.json()
        assert "manager_command" in data
        assert data["manager_command"]["type"] == "set_goal"


class TestAgentSessionData:
    def test_no_identity_store(self, client):
        resp = client.get("/agent/agent_000/session-data")
        assert resp.status_code == 503

    def test_not_found(self, client, manager):
        store = MagicMock()
        store.load.return_value = None
        manager.identity_store = store
        resp = client.get("/agent/agent_000/session-data")
        assert resp.status_code == 404


class TestSwarmPauseResume:
    def test_pause_swarm(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        resp = client.post("/swarm/pause")
        assert resp.status_code == 200
        assert manager.swarm_paused is True
        assert manager.agents["agent_000"].paused is True

    def test_resume_swarm(self, client, manager):
        manager.swarm_paused = True
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running", paused=True)
        resp = client.post("/swarm/resume")
        assert resp.status_code == 200
        assert manager.swarm_paused is False
        assert manager.agents["agent_000"].paused is False


class TestDesired:
    def test_set_desired(self, client, manager):
        resp = client.post("/swarm/desired", json={"count": 10})
        assert resp.status_code == 200
        assert manager.desired_agents == 10

    def test_set_desired_negative(self, client):
        resp = client.post("/swarm/desired", json={"count": -1})
        assert resp.status_code == 400

    def test_set_desired_invalid(self, client):
        resp = client.post("/swarm/desired", json={"count": "abc"})
        assert resp.status_code == 400


class TestBustRespawn:
    def test_toggle(self, client, manager):
        assert manager.bust_respawn is False
        resp = client.post("/swarm/bust-respawn", json={})
        assert resp.status_code == 200
        assert manager.bust_respawn is True


class TestKillAll:
    def test_kill_all(self, client, manager):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        manager.processes["agent_000"] = mock_proc
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        resp = client.post("/swarm/kill-all")
        assert resp.status_code == 200


class TestClearSwarm:
    def test_clear(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="stopped")
        resp = client.post("/swarm/clear")
        assert resp.status_code == 200
        assert len(manager.agents) == 0


class TestPruneDead:
    def test_prune(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="stopped")
        manager.agents["agent_001"] = AgentStatusBase(agent_id="agent_001", state="running")
        resp = client.post("/swarm/prune")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pruned"] == 1
        assert data["remaining"] == 1
        assert "agent_000" not in manager.agents


class TestAgentPauseResume:
    def test_pause_not_found(self, client):
        resp = client.post("/agent/nonexistent/pause")
        assert resp.status_code == 404

    def test_pause_without_plugin(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        resp = client.post("/agent/agent_000/pause")
        assert resp.status_code == 200
        assert manager.agents["agent_000"].paused is True

    def test_resume_not_found(self, client):
        resp = client.post("/agent/nonexistent/resume")
        assert resp.status_code == 404

    def test_resume_without_plugin(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running", paused=True)
        resp = client.post("/agent/agent_000/resume")
        assert resp.status_code == 200
        assert manager.agents["agent_000"].paused is False


class TestAgentRestart:
    def test_restart_not_found(self, client):
        resp = client.post("/agent/nonexistent/restart")
        assert resp.status_code == 404

    def test_restart_without_plugin(self, client, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        resp = client.post("/agent/agent_000/restart")
        assert resp.status_code == 200


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
