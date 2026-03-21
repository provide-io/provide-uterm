#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for manager/routes/bot_ops.py helper functions."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from provide.terminal.manager.app import create_manager_app
from provide.terminal.manager.config import ManagerConfig
from provide.terminal.manager.models import BotStatusBase
from provide.terminal.manager.routes.bot_ops import (
    _append_command_history,
    _build_action_response,
    _cancel_pending_manager_command,
    _command_history_rows,
    _queue_manager_command,
    _update_command_history,
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
# _command_history_rows mutation killers
# mutmut_6: getattr(bot_status, "manager_command_history",) — missing None default
#           This causes a TypeError when manager_command_history is not set
# ---------------------------------------------------------------------------


class TestCommandHistoryRowsMutationKilling:
    def test_returns_empty_list_when_no_attribute(self):
        """When manager_command_history is not set on an ad-hoc object, returns [].
        Uses a SimpleNamespace to avoid BotStatusBase always having the field.
        (mutmut_6: missing None default — getattr with no default raises AttributeError
        when attribute is absent; with None default it returns None → non-list → [].)"""
        import types

        obj = types.SimpleNamespace(bot_id="x")
        assert not hasattr(obj, "manager_command_history")
        rows = _command_history_rows(obj)  # type: ignore[arg-type]
        assert rows == []
        assert obj.manager_command_history == []

    def test_returns_existing_list(self, bot):
        """When manager_command_history is already a list, returns it."""
        bot.manager_command_history = [{"seq": 1}]
        rows = _command_history_rows(bot)
        assert rows == [{"seq": 1}]

    def test_replaces_non_list_with_empty_list(self, bot):
        """When manager_command_history is not a list, replaces with []."""
        bot.manager_command_history = "not a list"  # type: ignore[assignment]
        rows = _command_history_rows(bot)
        assert rows == []
        assert bot.manager_command_history == []  # type: ignore[comparison-overlap]


# ---------------------------------------------------------------------------
# _append_command_history mutation killers
# mutmut_5: >= 25 instead of > 25 (should trim at 26 entries not 25)
# ---------------------------------------------------------------------------


class TestAppendCommandHistoryMutationKilling:
    def test_no_trim_at_exactly_25(self, bot):
        """At exactly 25 entries, no trim occurs (mutmut_5: >= would trim at 25)."""
        # Pre-fill with 24 entries
        bot.manager_command_history = [{"seq": i} for i in range(24)]
        _append_command_history(bot, {"seq": 25})
        # Should now have 25 — no trim
        assert len(bot.manager_command_history) == 25

    def test_trim_at_26(self, bot):
        """At 26 entries (after append), trims to keep last 25."""
        bot.manager_command_history = [{"seq": i} for i in range(25)]
        _append_command_history(bot, {"seq": 26})
        assert len(bot.manager_command_history) == 25
        # Most recent entry must be kept
        assert bot.manager_command_history[-1]["seq"] == 26

    def test_trim_keeps_most_recent_25(self, bot):
        """After overflow, the oldest entry is dropped."""
        bot.manager_command_history = [{"seq": i} for i in range(25)]
        _append_command_history(bot, {"seq": 100})
        # seq 0 (oldest) should be gone
        seqs = [r["seq"] for r in bot.manager_command_history]
        assert 0 not in seqs
        assert 100 in seqs

    def test_entry_is_copied_not_referenced(self, bot):
        """Entry is stored as a copy (dict(entry))."""
        entry = {"seq": 1, "extra": "data"}
        _append_command_history(bot, entry)
        entry["extra"] = "modified"
        assert bot.manager_command_history[0]["extra"] == "data"


# ---------------------------------------------------------------------------
# _update_command_history mutation killers
# mutmut_1: seq < 0 instead of seq <= 0 (seq=0 should NOT update)
# mutmut_2: seq <= 1 instead of seq <= 0 (seq=1 should update but mutant skips it)
# mutmut_10: row.get("seq") or 1 instead of or 0 (changes zero-seq fallback)
# mutmut_12: break instead of continue (stops iteration after first non-match)
# ---------------------------------------------------------------------------


class TestUpdateCommandHistoryMutationKilling:
    def test_seq_zero_does_not_update(self, bot):
        """seq=0 should do nothing (return early). mutmut_1 (seq<0) would update seq=0."""
        bot.manager_command_history = [{"seq": 0, "status": "original"}]
        _update_command_history(bot, 0, status="changed")
        # seq <= 0 → return early, nothing updated
        assert bot.manager_command_history[0]["status"] == "original"

    def test_seq_one_updates(self, bot):
        """seq=1 should update the matching row. mutmut_2 (<=1) would skip seq=1."""
        bot.manager_command_history = [{"seq": 1, "status": "queued"}]
        _update_command_history(bot, 1, status="done")
        assert bot.manager_command_history[0]["status"] == "done"

    def test_updates_correct_row_when_multiple(self, bot):
        """Only the row with matching seq is updated (mutmut_12: break would stop early)."""
        bot.manager_command_history = [
            {"seq": 1, "status": "queued"},
            {"seq": 2, "status": "queued"},
            {"seq": 3, "status": "queued"},
        ]
        _update_command_history(bot, 2, status="done")
        assert bot.manager_command_history[0]["status"] == "queued"  # unchanged
        assert bot.manager_command_history[1]["status"] == "done"  # updated
        assert bot.manager_command_history[2]["status"] == "queued"  # unchanged

    def test_seq_with_zero_fallback_matches_only_nonzero(self, bot):
        """row.get("seq") or 0 means missing/falsy seq treated as 0.
        mutmut_10 uses 'or 1' which treats missing seq as 1, corrupting matches."""
        # Row with no seq key
        bot.manager_command_history = [{"status": "original"}]
        _update_command_history(bot, 1, status="updated")
        # seq from row defaults to 0 via 'or 0', which != 1 → row not updated
        assert bot.manager_command_history[0]["status"] == "original"

    def test_search_reversed_finds_latest_first(self, bot):
        """Search reversed: if multiple rows have same seq, updates most recent."""
        bot.manager_command_history = [
            {"seq": 5, "status": "old"},
            {"seq": 5, "status": "newer"},
        ]
        _update_command_history(bot, 5, status="latest")
        # reversed() starts from index 1 (the newer one)
        assert bot.manager_command_history[1]["status"] == "latest"
        assert bot.manager_command_history[0]["status"] == "old"

    def test_break_vs_continue_behavior(self, bot):
        """mutmut_12 uses break instead of continue.
        With break: first non-matching row stops iteration, later rows never checked.
        Test: seq=3 in first position should still be found with correct behavior."""
        bot.manager_command_history = [
            {"seq": 1, "status": "q"},
            {"seq": 2, "status": "q"},
            {"seq": 3, "status": "q"},
        ]
        _update_command_history(bot, 3, status="found")
        # With original (continue): reversed = [3, 2, 1] → finds seq 3 at index 0
        assert bot.manager_command_history[2]["status"] == "found"


# ---------------------------------------------------------------------------
# _queue_manager_command mutation killers
# Tests that seq increments correctly and all fields set properly
# ---------------------------------------------------------------------------


class TestQueueManagerCommandMutationKilling:
    def test_first_command_gets_seq_1(self, bot):
        """First command has seq=1 (replaces 0+1)."""
        result = _queue_manager_command(bot, "set_goal", {"goal": "explore"})
        assert result["seq"] == 1
        assert bot.pending_command_seq == 1

    def test_second_command_gets_seq_2(self, bot):
        """Second command gets seq=2."""
        _queue_manager_command(bot, "set_goal", {"goal": "explore"})
        result = _queue_manager_command(bot, "set_goal", {"goal": "fight"})
        assert result["seq"] == 2
        assert bot.pending_command_seq == 2

    def test_type_stored(self, bot):
        """pending_command_type is set correctly."""
        _queue_manager_command(bot, "set_directive", {"directive": "be cautious"})
        assert bot.pending_command_type == "set_directive"

    def test_payload_copied(self, bot):
        """payload is stored as copy."""
        payload = {"goal": "test"}
        _queue_manager_command(bot, "set_goal", payload)
        payload["goal"] = "modified"
        assert bot.pending_command_payload["goal"] == "test"

    def test_replaces_field_none_for_first(self, bot):
        """First command has replaces=None (no previous seq)."""
        result = _queue_manager_command(bot, "set_goal", {"goal": "x"})
        assert result["replaces"] is None

    def test_replaces_field_set_for_second(self, bot):
        """Second command has replaces=1 (replaced the first)."""
        _queue_manager_command(bot, "set_goal", {"goal": "x"})
        result = _queue_manager_command(bot, "set_goal", {"goal": "y"})
        assert result["replaces"] == 1

    def test_history_entry_added(self, bot):
        """Command history entry added with status='queued'."""
        _queue_manager_command(bot, "set_goal", {"goal": "x"})
        rows = _command_history_rows(bot)
        assert len(rows) == 1
        assert rows[0]["status"] == "queued"
        assert rows[0]["seq"] == 1

    def test_replaced_command_marked_in_history(self, bot):
        """When replacing, previous command is updated to status='replaced'."""
        _queue_manager_command(bot, "set_goal", {"goal": "x"})
        _queue_manager_command(bot, "set_goal", {"goal": "y"})
        rows = _command_history_rows(bot)
        # First row should be marked replaced
        first = next(r for r in rows if r["seq"] == 1)
        assert first["status"] == "replaced"

    def test_result_contains_type(self, bot):
        """Result dict contains 'type' field."""
        result = _queue_manager_command(bot, "set_goal", {"goal": "z"})
        assert result["type"] == "set_goal"

    def test_result_contains_payload(self, bot):
        """Result dict contains 'payload' field."""
        result = _queue_manager_command(bot, "set_goal", {"goal": "z"})
        assert result["payload"] == {"goal": "z"}


# ---------------------------------------------------------------------------
# _cancel_pending_manager_command mutation killers
# mutmut_1: pending_seq < 0 check
# mutmut_2: pending_seq <= 1
# mutmut_8: pending_type check broken
# mutmut_11: seq stored in cancelled incorrectly
# mutmut_14, 15: type stored incorrectly
# mutmut_17: payload extracted incorrectly
# mutmut_21: cancelled_reason not stored
# mutmut_24: pending_command_seq not set to 0
# mutmut_27-30: pending_command_type/payload/seq not cleared
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
# mutmut_2-36: various field mutations when plugin=None
# Key: test that all fields present in result dict with correct values
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
        # Queue a command
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
