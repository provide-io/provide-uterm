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

    def test_register_rejected_at_max_agents(self, client, manager):
        """Auto-creating a new record must honor max_agents (PLAT-reg)."""
        manager.max_agents = 2
        manager.agents["a0"] = AgentStatusBase(agent_id="a0", state="running")
        manager.agents["a1"] = AgentStatusBase(agent_id="a1", state="running")
        resp = client.post("/agent/a2/register", json={"state": "running"})
        assert resp.status_code == 429
        assert "a2" not in manager.agents

    def test_register_existing_allowed_at_max_agents(self, client, manager):
        """An update to an already-known agent at the cap is still allowed."""
        manager.max_agents = 2
        manager.agents["a0"] = AgentStatusBase(agent_id="a0", state="running")
        manager.agents["a1"] = AgentStatusBase(agent_id="a1", state="running")
        resp = client.post("/agent/a1/register", json={"state": "stopped"})
        assert resp.status_code == 200
        assert resp.json()["created"] is False


class TestAgentRegisterPrivEsc:
    """Regression tests for the worker-token register privilege-escalation.

    A low-privilege worker token is accepted on ``POST /agent/{id}/register``
    (a self-report route). Before the fix, that route merged the FULL
    attacker-controlled body over ``AgentStatusBase``, letting a worker set
    operator-authority command-queue fields (``pending_command_*``) — i.e.
    inject a ``set_goal``/``set_directive`` operator command into ANY agent's
    queue, which the ``status`` poll then delivers to the agent runtime.
    """

    @pytest.mark.parametrize(
        "field",
        [
            "pending_command_seq",
            "pending_command_type",
            "pending_command_payload",
            "manager_command_history",
            "is_hijacked",
            "hijacked_by",
            "hijacked_at",
            "paused",
        ],
    )
    def test_register_rejects_operator_fields(self, client, manager, field):
        """A worker register that sets any operator-authority field is 422'd."""
        injected = {
            "pending_command_seq": 1,
            "pending_command_type": "set_goal",
            "pending_command_payload": {"goal": "PWNED"},
            "manager_command_history": [{"seq": 1}],
            "is_hijacked": True,
            "hijacked_by": "attacker",
            "hijacked_at": 1.0,
            "paused": True,
        }[field]
        resp = client.post("/agent/victim/register", json={"state": "running", field: injected})
        assert resp.status_code == 422
        # The agent must NOT have been created from a rejected register.
        assert "victim" not in manager.agents

    def test_register_command_injection_rejected(self, client, manager):
        """The full command-injection body (the exploit) is rejected outright."""
        resp = client.post(
            "/agent/victim/register",
            json={
                "state": "running",
                "pending_command_type": "set_goal",
                "pending_command_seq": 1,
                "pending_command_payload": {"goal": "PWNED"},
            },
        )
        assert resp.status_code == 422
        assert "victim" not in manager.agents

    def test_register_command_injection_on_existing_agent_rejected(self, client, manager):
        """Worker register cannot overwrite an existing agent's command queue."""
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        resp = client.post(
            "/agent/agent_000/register",
            json={"state": "stopped", "pending_command_type": "set_goal", "pending_command_seq": 1},
        )
        assert resp.status_code == 422
        # Existing record left untouched (no injected command).
        assert manager.agents["agent_000"].pending_command_type is None
        assert manager.agents["agent_000"].pending_command_seq == 0

    def test_register_preserves_operator_set_pending_command(self, client, manager):
        """An operator-set pending command survives a benign worker register.

        ``base_payload`` carries the agent's real ``pending_command_*`` from
        the stored record; because ``register`` no longer reads those keys from
        the worker body, a worker self-report that omits them leaves the
        operator's queued command intact.
        """
        agent = AgentStatusBase(agent_id="agent_000", state="running")
        agent.pending_command_seq = 7
        agent.pending_command_type = "set_directive"
        agent.pending_command_payload = {"directive": "hold", "turns": 3}
        manager.agents["agent_000"] = agent
        resp = client.post("/agent/agent_000/register", json={"state": "recovering"})
        assert resp.status_code == 200
        updated = manager.agents["agent_000"]
        assert updated.state == "recovering"
        assert updated.pending_command_seq == 7
        assert updated.pending_command_type == "set_directive"
        assert updated.pending_command_payload == {"directive": "hold", "turns": 3}

    def test_legitimate_self_report_still_works(self, client, manager):
        """A worker register with only allowed self-report fields succeeds."""
        resp = client.post(
            "/agent/agent_self/register",
            json={"state": "running", "session_id": "sess-1", "pid": 4242, "last_action": "trade"},
        )
        assert resp.status_code == 200
        assert resp.json()["created"] is True
        created = manager.agents["agent_self"]
        assert created.state == "running"
        assert created.session_id == "sess-1"
        assert created.pid == 4242
        assert created.last_action == "trade"
        # Operator fields default — not set by the worker.
        assert created.pending_command_seq == 0
        assert created.pending_command_type is None

    def test_operator_set_goal_still_queues_command(self, client, manager):
        """The operator set-goal route is unchanged and still queues a command."""
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        resp = client.post("/agent/agent_000/set-goal", params={"goal": "explore"})
        assert resp.status_code == 200
        agent = manager.agents["agent_000"]
        assert agent.pending_command_type == "set_goal"
        assert agent.pending_command_seq == 1
        assert agent.pending_command_payload == {"goal": "explore"}
        # And the next status poll delivers it as a manager command.
        poll = client.post("/agent/agent_000/status", json={"state": "running"})
        assert poll.json()["manager_command"]["type"] == "set_goal"

    @pytest.mark.parametrize(
        "bad_id",
        ["weird id", "bad$char", "with space", "dot.char"],
    )
    def test_register_rejects_bad_agent_id(self, client, manager, bad_id):
        """Non-slash bad chars are 422'd by the agent_id pattern (^[\\w\\-]+$).

        This is parity with the status route, which already had the pattern.
        """
        resp = client.post(f"/agent/{bad_id}/register", json={"state": "running"})
        assert resp.status_code == 422
        # Parity check: the sibling status route rejects the same id identically.
        assert client.post(f"/agent/{bad_id}/status", json={"state": "running"}).status_code == 422

    @pytest.mark.parametrize("bad_id", ["a/b", "..%2f..%2fx"])
    def test_register_rejects_slash_agent_id(self, client, manager, bad_id):
        """Slash-containing ids never reach the handler (404 at routing), same
        as the status route — neither path-injects nor auto-creates an agent."""
        resp = client.post(f"/agent/{bad_id}/register", json={"state": "running"})
        assert resp.status_code == 404
        assert manager.agents == {}

    def test_register_accepts_valid_agent_id(self, client, manager):
        """A well-formed agent_id (word chars + hyphen) is accepted."""
        resp = client.post("/agent/agent-007_x/register", json={"state": "running"})
        assert resp.status_code == 200
        assert "agent-007_x" in manager.agents


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

    def test_status_auto_create_rejected_at_max_agents(self, client, manager):
        """Status auto-create for a new id must honor max_agents (PLAT-reg)."""
        manager.max_agents = 2
        manager.agents["a0"] = AgentStatusBase(agent_id="a0", state="running")
        manager.agents["a1"] = AgentStatusBase(agent_id="a1", state="running")
        resp = client.post("/agent/a2/status", json={"state": "running"})
        assert resp.status_code == 429
        assert "a2" not in manager.agents

    def test_status_known_agent_allowed_at_max_agents(self, client, manager):
        """A status update for an already-known agent at the cap is allowed."""
        manager.max_agents = 2
        manager.agents["a0"] = AgentStatusBase(agent_id="a0", state="running")
        manager.agents["a1"] = AgentStatusBase(agent_id="a1", state="running")
        resp = client.post("/agent/a1/status", json={"state": "recovering"})
        assert resp.status_code == 200
        assert manager.agents["a1"].state == "recovering"

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
