#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for manager/routes/agent_ops.py helper functions (part 2).

Classes: TestCancelPendingCommandMutationKilling, TestBuildActionResponseMutationKilling,
         TestAgentOpsRouteMutationKilling.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from provide.terminal.manager.app import create_manager_app
from provide.terminal.manager.config import ManagerConfig
from provide.terminal.manager.models import AgentStatusBase
from provide.terminal.manager.routes.agent_ops import (
    _build_action_response,
    _cancel_pending_manager_command,
    _command_history_rows,
    _queue_manager_command,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def agent() -> AgentStatusBase:
    return AgentStatusBase(agent_id="agent_001", state="running")


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


# ---------------------------------------------------------------------------
# _cancel_pending_manager_command mutation killers
# ---------------------------------------------------------------------------


class TestCancelPendingCommandMutationKilling:
    def test_returns_none_when_no_pending(self, agent):
        """Returns None when no pending command."""
        result = _cancel_pending_manager_command(agent)
        assert result is None

    def test_returns_none_when_seq_zero(self, agent):
        """Returns None when pending_seq is 0 (mutmut_1: < 0 would return None for seq=0 but execute for seq=0)."""
        agent.pending_command_seq = 0
        agent.pending_command_type = None
        result = _cancel_pending_manager_command(agent)
        assert result is None

    def test_returns_none_when_type_empty(self, agent):
        """Returns None when pending_type is empty (mutmut_8 may skip type check)."""
        agent.pending_command_seq = 1
        agent.pending_command_type = ""
        result = _cancel_pending_manager_command(agent)
        assert result is None

    def test_cancel_returns_correct_seq(self, agent):
        """Cancelled dict has correct seq (mutmut_11)."""
        agent.pending_command_seq = 3
        agent.pending_command_type = "set_goal"
        agent.pending_command_payload = {"goal": "x"}
        result = _cancel_pending_manager_command(agent)
        assert result is not None
        assert result["seq"] == 3

    def test_cancel_returns_correct_type(self, agent):
        """Cancelled dict has correct type (mutmut_14/15)."""
        agent.pending_command_seq = 1
        agent.pending_command_type = "set_directive"
        agent.pending_command_payload = {}
        result = _cancel_pending_manager_command(agent)
        assert result is not None
        assert result["type"] == "set_directive"

    def test_cancel_returns_correct_payload(self, agent):
        """Cancelled dict has correct payload (mutmut_17)."""
        agent.pending_command_seq = 1
        agent.pending_command_type = "set_goal"
        agent.pending_command_payload = {"goal": "explore", "extra": 42}
        result = _cancel_pending_manager_command(agent)
        assert result is not None
        assert result["payload"] == {"goal": "explore", "extra": 42}

    def test_cancel_returns_reason(self, agent):
        """Cancelled dict has cancelled_reason (mutmut_21)."""
        agent.pending_command_seq = 1
        agent.pending_command_type = "set_goal"
        agent.pending_command_payload = {}
        result = _cancel_pending_manager_command(agent, "test_reason")
        assert result is not None
        assert result["cancelled_reason"] == "test_reason"

    def test_cancel_clears_pending_seq(self, agent):
        """pending_command_seq reset to 0 after cancel (mutmut_24)."""
        agent.pending_command_seq = 5
        agent.pending_command_type = "set_goal"
        agent.pending_command_payload = {}
        _cancel_pending_manager_command(agent)
        assert agent.pending_command_seq == 0

    def test_cancel_clears_pending_type(self, agent):
        """pending_command_type reset to None after cancel (mutmut_27-29)."""
        agent.pending_command_seq = 1
        agent.pending_command_type = "set_goal"
        agent.pending_command_payload = {}
        _cancel_pending_manager_command(agent)
        assert agent.pending_command_type is None

    def test_cancel_clears_pending_payload(self, agent):
        """pending_command_payload reset to {} after cancel."""
        agent.pending_command_seq = 1
        agent.pending_command_type = "set_goal"
        agent.pending_command_payload = {"goal": "x"}
        _cancel_pending_manager_command(agent)
        assert agent.pending_command_payload == {}

    def test_cancel_default_reason(self, agent):
        """Default reason is 'operator_cancelled'."""
        agent.pending_command_seq = 1
        agent.pending_command_type = "set_goal"
        agent.pending_command_payload = {}
        result = _cancel_pending_manager_command(agent)
        assert result is not None
        assert result["cancelled_reason"] == "operator_cancelled"

    def test_cancel_updates_history_to_cancelled(self, agent):
        """History entry for the seq is updated to status=cancelled."""
        _queue_manager_command(agent, "set_goal", {"goal": "x"})
        _cancel_pending_manager_command(agent)
        rows = _command_history_rows(agent)
        assert rows[0]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# _build_action_response mutation killers
# ---------------------------------------------------------------------------


class TestBuildActionResponseMutationKilling:
    def test_returns_all_fields_without_plugin(self):
        """Without plugin, returns dict with all required fields."""
        result = _build_action_response(
            "agent_001",
            "set_goal",
            "manager",
            applied=True,
            queued=False,
            result={"goal": "explore"},
            state="running",
        )
        assert result["agent_id"] == "agent_001"
        assert result["action"] == "set_goal"
        assert result["source"] == "manager"
        assert result["applied"] is True
        assert result["queued"] is False
        assert result["result"] == {"goal": "explore"}
        assert result["state"] == "running"

    def test_agent_id_in_result(self):
        """agent_id field present and correct (mutmut_27-28 may change it)."""
        result = _build_action_response(
            "unique_agent_xyz",
            "action",
            "src",
            applied=False,
            queued=True,
            result={},
            state="idle",
        )
        assert result["agent_id"] == "unique_agent_xyz"

    def test_action_in_result(self):
        """action field present and correct."""
        result = _build_action_response(
            "agent",
            "custom_action_type",
            "src",
            applied=False,
            queued=False,
            result={},
            state="idle",
        )
        assert result["action"] == "custom_action_type"

    def test_source_in_result(self):
        """source field present and correct."""
        result = _build_action_response(
            "agent",
            "act",
            "worker_queue_source",
            applied=False,
            queued=True,
            result={},
            state="idle",
        )
        assert result["source"] == "worker_queue_source"

    def test_applied_true_persists(self):
        """applied=True is stored correctly."""
        result = _build_action_response("b", "a", "s", applied=True, queued=False, result={}, state="x")
        assert result["applied"] is True

    def test_applied_false_persists(self):
        """applied=False is stored correctly."""
        result = _build_action_response("b", "a", "s", applied=False, queued=True, result={}, state="x")
        assert result["applied"] is False

    def test_queued_true_persists(self):
        """queued=True is stored correctly."""
        result = _build_action_response("b", "a", "s", applied=False, queued=True, result={}, state="x")
        assert result["queued"] is True

    def test_result_dict_preserved(self):
        """result dict is stored correctly."""
        inner = {"key": "value", "num": 42}
        result = _build_action_response("b", "a", "s", applied=True, queued=False, result=inner, state="x")
        assert result["result"] == {"key": "value", "num": 42}

    def test_state_preserved(self):
        """state string is stored correctly."""
        result = _build_action_response("b", "a", "s", applied=True, queued=False, result={}, state="running")
        assert result["state"] == "running"

    def test_plugin_branch_calls_plugin(self):
        """With plugin, calls plugin.build_action_response."""
        plugin = MagicMock()
        plugin.build_action_response.return_value = {"plugin_result": True}
        result = _build_action_response(
            "agent",
            "act",
            "src",
            applied=True,
            queued=False,
            result={},
            state="running",
            plugin=plugin,
        )
        plugin.build_action_response.assert_called_once_with(
            "agent", "act", "src", applied=True, queued=False, result={}, state="running"
        )
        assert result == {"plugin_result": True}


# ---------------------------------------------------------------------------
# Route integration tests for mutation-killing
# ---------------------------------------------------------------------------


class TestAgentOpsRouteMutationKilling:
    def test_cancel_command_with_pending(self, client, manager):
        """cancel-command route updates history and returns cancelled info."""
        manager.agents["agent_A"] = AgentStatusBase(agent_id="agent_A", state="running")
        _queue_manager_command(manager.agents["agent_A"], "set_goal", {"goal": "test"})

        resp = client.post("/agent/agent_A/cancel-command")
        assert resp.status_code == 200
        data = resp.json()
        assert data["applied"] is True
        assert "cancelled" in data["result"]

    def test_set_goal_queues_command(self, client, manager):
        """set-goal route queues a command and returns queued info."""
        manager.agents["agent_B"] = AgentStatusBase(agent_id="agent_B", state="running")
        resp = client.post("/agent/agent_B/set-goal?goal=conquer")
        assert resp.status_code == 200
        data = resp.json()
        assert data["queued"] is True
        assert data["agent_id"] == "agent_B"
        assert data["action"] == "set_goal"

    def test_set_directive_queues_command(self, client, manager):
        """set-directive route queues a command."""
        manager.agents["agent_C"] = AgentStatusBase(agent_id="agent_C", state="running")
        resp = client.post("/agent/agent_C/set-directive", json={"directive": "be cautious", "turns": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert data["queued"] is True
        assert data["action"] == "set_directive"

    def test_cancel_command_no_pending_returns_not_applied(self, client, manager):
        """cancel-command with no pending returns applied=False."""
        manager.agents["agent_D"] = AgentStatusBase(agent_id="agent_D", state="running")
        resp = client.post("/agent/agent_D/cancel-command")
        assert resp.status_code == 200
        data = resp.json()
        assert data["applied"] is False

    def test_kill_agent_with_process_desired_agents_zero_not_decremented(self, client, manager):
        """Branch 375->377 (False): desired_agents==0 when killing an agent with a process.

        Line 375: `if manager.desired_agents > 0:` is False — desired_agents stays 0,
        not decremented further. The kill response is still returned normally.
        """
        from unittest.mock import MagicMock, patch

        from provide.terminal.manager.process import AgentProcessManager

        manager.agents["agent_E"] = AgentStatusBase(agent_id="agent_E", state="running")
        proc = MagicMock()
        proc.poll.return_value = None
        manager.processes["agent_E"] = proc
        manager.desired_agents = 0  # already zero — branch 375->377 is False

        with patch.object(AgentProcessManager, "_stop_process_tree", return_value=None):
            resp = client.delete("/agent/agent_E")

        assert resp.status_code == 200
        data = resp.json()
        assert data["applied"] is True
        # desired_agents was 0 and was NOT decremented (stays 0)
        assert manager.desired_agents == 0

    def test_kill_agent_with_process_desired_agents_positive_decremented(self, client, manager):
        """Branch 375->377 (True): desired_agents>0 when killing an agent with a process.

        Contrasting test: desired_agents=3, kill removes one → desired_agents decremented to 2.
        """
        from unittest.mock import MagicMock, patch

        from provide.terminal.manager.process import AgentProcessManager

        manager.agents["agent_F"] = AgentStatusBase(agent_id="agent_F", state="running")
        proc = MagicMock()
        proc.poll.return_value = None
        manager.processes["agent_F"] = proc
        manager.desired_agents = 3

        with patch.object(AgentProcessManager, "_stop_process_tree", return_value=None):
            resp = client.delete("/agent/agent_F")

        assert resp.status_code == 200
        assert manager.desired_agents == 2
