#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for manager/routes/bot_ops.py helper functions (part 2).

Classes: TestCancelPendingCommandMutationKilling, TestBuildActionResponseMutationKilling,
         TestBotOpsRouteMutationKilling.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from provide.terminal.manager.app import create_manager_app
from provide.terminal.manager.config import ManagerConfig
from provide.terminal.manager.models import BotStatusBase
from provide.terminal.manager.routes.bot_ops import (
    _build_action_response,
    _cancel_pending_manager_command,
    _command_history_rows,
    _queue_manager_command,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bot() -> BotStatusBase:
    return BotStatusBase(bot_id="bot_001", state="running")


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
    def test_returns_none_when_no_pending(self, bot):
        """Returns None when no pending command."""
        result = _cancel_pending_manager_command(bot)
        assert result is None

    def test_returns_none_when_seq_zero(self, bot):
        """Returns None when pending_seq is 0 (mutmut_1: < 0 would return None for seq=0 but execute for seq=0)."""
        bot.pending_command_seq = 0
        bot.pending_command_type = None
        result = _cancel_pending_manager_command(bot)
        assert result is None

    def test_returns_none_when_type_empty(self, bot):
        """Returns None when pending_type is empty (mutmut_8 may skip type check)."""
        bot.pending_command_seq = 1
        bot.pending_command_type = ""
        result = _cancel_pending_manager_command(bot)
        assert result is None

    def test_cancel_returns_correct_seq(self, bot):
        """Cancelled dict has correct seq (mutmut_11)."""
        bot.pending_command_seq = 3
        bot.pending_command_type = "set_goal"
        bot.pending_command_payload = {"goal": "x"}
        result = _cancel_pending_manager_command(bot)
        assert result is not None
        assert result["seq"] == 3

    def test_cancel_returns_correct_type(self, bot):
        """Cancelled dict has correct type (mutmut_14/15)."""
        bot.pending_command_seq = 1
        bot.pending_command_type = "set_directive"
        bot.pending_command_payload = {}
        result = _cancel_pending_manager_command(bot)
        assert result is not None
        assert result["type"] == "set_directive"

    def test_cancel_returns_correct_payload(self, bot):
        """Cancelled dict has correct payload (mutmut_17)."""
        bot.pending_command_seq = 1
        bot.pending_command_type = "set_goal"
        bot.pending_command_payload = {"goal": "explore", "extra": 42}
        result = _cancel_pending_manager_command(bot)
        assert result is not None
        assert result["payload"] == {"goal": "explore", "extra": 42}

    def test_cancel_returns_reason(self, bot):
        """Cancelled dict has cancelled_reason (mutmut_21)."""
        bot.pending_command_seq = 1
        bot.pending_command_type = "set_goal"
        bot.pending_command_payload = {}
        result = _cancel_pending_manager_command(bot, "test_reason")
        assert result is not None
        assert result["cancelled_reason"] == "test_reason"

    def test_cancel_clears_pending_seq(self, bot):
        """pending_command_seq reset to 0 after cancel (mutmut_24)."""
        bot.pending_command_seq = 5
        bot.pending_command_type = "set_goal"
        bot.pending_command_payload = {}
        _cancel_pending_manager_command(bot)
        assert bot.pending_command_seq == 0

    def test_cancel_clears_pending_type(self, bot):
        """pending_command_type reset to None after cancel (mutmut_27-29)."""
        bot.pending_command_seq = 1
        bot.pending_command_type = "set_goal"
        bot.pending_command_payload = {}
        _cancel_pending_manager_command(bot)
        assert bot.pending_command_type is None

    def test_cancel_clears_pending_payload(self, bot):
        """pending_command_payload reset to {} after cancel."""
        bot.pending_command_seq = 1
        bot.pending_command_type = "set_goal"
        bot.pending_command_payload = {"goal": "x"}
        _cancel_pending_manager_command(bot)
        assert bot.pending_command_payload == {}

    def test_cancel_default_reason(self, bot):
        """Default reason is 'operator_cancelled'."""
        bot.pending_command_seq = 1
        bot.pending_command_type = "set_goal"
        bot.pending_command_payload = {}
        result = _cancel_pending_manager_command(bot)
        assert result is not None
        assert result["cancelled_reason"] == "operator_cancelled"

    def test_cancel_updates_history_to_cancelled(self, bot):
        """History entry for the seq is updated to status=cancelled."""
        _queue_manager_command(bot, "set_goal", {"goal": "x"})
        _cancel_pending_manager_command(bot)
        rows = _command_history_rows(bot)
        assert rows[0]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# _build_action_response mutation killers
# ---------------------------------------------------------------------------


class TestBuildActionResponseMutationKilling:
    def test_returns_all_fields_without_plugin(self):
        """Without plugin, returns dict with all required fields."""
        result = _build_action_response(
            "bot_001",
            "set_goal",
            "manager",
            applied=True,
            queued=False,
            result={"goal": "explore"},
            state="running",
        )
        assert result["bot_id"] == "bot_001"
        assert result["action"] == "set_goal"
        assert result["source"] == "manager"
        assert result["applied"] is True
        assert result["queued"] is False
        assert result["result"] == {"goal": "explore"}
        assert result["state"] == "running"

    def test_bot_id_in_result(self):
        """bot_id field present and correct (mutmut_27-28 may change it)."""
        result = _build_action_response(
            "unique_bot_xyz",
            "action",
            "src",
            applied=False,
            queued=True,
            result={},
            state="idle",
        )
        assert result["bot_id"] == "unique_bot_xyz"

    def test_action_in_result(self):
        """action field present and correct."""
        result = _build_action_response(
            "bot",
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
            "bot",
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
            "bot",
            "act",
            "src",
            applied=True,
            queued=False,
            result={},
            state="running",
            plugin=plugin,
        )
        plugin.build_action_response.assert_called_once_with(
            "bot", "act", "src", applied=True, queued=False, result={}, state="running"
        )
        assert result == {"plugin_result": True}


# ---------------------------------------------------------------------------
# Route integration tests for mutation-killing
# ---------------------------------------------------------------------------


class TestBotOpsRouteMutationKilling:
    def test_cancel_command_with_pending(self, client, manager):
        """cancel-command route updates history and returns cancelled info."""
        manager.bots["bot_A"] = BotStatusBase(bot_id="bot_A", state="running")
        _queue_manager_command(manager.bots["bot_A"], "set_goal", {"goal": "test"})

        resp = client.post("/bot/bot_A/cancel-command")
        assert resp.status_code == 200
        data = resp.json()
        assert data["applied"] is True
        assert "cancelled" in data["result"]

    def test_set_goal_queues_command(self, client, manager):
        """set-goal route queues a command and returns queued info."""
        manager.bots["bot_B"] = BotStatusBase(bot_id="bot_B", state="running")
        resp = client.post("/bot/bot_B/set-goal?goal=conquer")
        assert resp.status_code == 200
        data = resp.json()
        assert data["queued"] is True
        assert data["bot_id"] == "bot_B"
        assert data["action"] == "set_goal"

    def test_set_directive_queues_command(self, client, manager):
        """set-directive route queues a command."""
        manager.bots["bot_C"] = BotStatusBase(bot_id="bot_C", state="running")
        resp = client.post("/bot/bot_C/set-directive", json={"directive": "be cautious", "turns": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert data["queued"] is True
        assert data["action"] == "set_directive"

    def test_cancel_command_no_pending_returns_not_applied(self, client, manager):
        """cancel-command with no pending returns applied=False."""
        manager.bots["bot_D"] = BotStatusBase(bot_id="bot_D", state="running")
        resp = client.post("/bot/bot_D/cancel-command")
        assert resp.status_code == 200
        data = resp.json()
        assert data["applied"] is False

    def test_kill_bot_with_process_desired_bots_zero_not_decremented(self, client, manager):
        """Branch 375->377 (False): desired_bots==0 when killing a bot with a process.

        Line 375: `if manager.desired_bots > 0:` is False — desired_bots stays 0,
        not decremented further. The kill response is still returned normally.
        """
        from unittest.mock import MagicMock, patch

        from provide.terminal.manager.process import BotProcessManager

        manager.bots["bot_E"] = BotStatusBase(bot_id="bot_E", state="running")
        proc = MagicMock()
        proc.poll.return_value = None
        manager.processes["bot_E"] = proc
        manager.desired_bots = 0  # already zero — branch 375->377 is False

        with patch.object(BotProcessManager, "_stop_process_tree", return_value=None):
            resp = client.delete("/bot/bot_E")

        assert resp.status_code == 200
        data = resp.json()
        assert data["applied"] is True
        # desired_bots was 0 and was NOT decremented (stays 0)
        assert manager.desired_bots == 0

    def test_kill_bot_with_process_desired_bots_positive_decremented(self, client, manager):
        """Branch 375->377 (True): desired_bots>0 when killing a bot with a process.

        Contrasting test: desired_bots=3, kill removes one → desired_bots decremented to 2.
        """
        from unittest.mock import MagicMock, patch

        from provide.terminal.manager.process import BotProcessManager

        manager.bots["bot_F"] = BotStatusBase(bot_id="bot_F", state="running")
        proc = MagicMock()
        proc.poll.return_value = None
        manager.processes["bot_F"] = proc
        manager.desired_bots = 3

        with patch.object(BotProcessManager, "_stop_process_tree", return_value=None):
            resp = client.delete("/bot/bot_F")

        assert resp.status_code == 200
        assert manager.desired_bots == 2
