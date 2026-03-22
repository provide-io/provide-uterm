#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for manager/routes/bot_ops.py helper functions (part 1).

Classes: TestCommandHistoryRowsMutationKilling, TestAppendCommandHistoryMutationKilling,
         TestUpdateCommandHistoryMutationKilling, TestQueueManagerCommandMutationKilling.
"""

from __future__ import annotations

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
# ---------------------------------------------------------------------------


class TestCommandHistoryRowsMutationKilling:
    def test_returns_empty_list_when_no_attribute(self):
        """When manager_command_history is not set on an ad-hoc object, returns [].
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
        bot.manager_command_history = [{"seq": i} for i in range(24)]
        _append_command_history(bot, {"seq": 25})
        assert len(bot.manager_command_history) == 25

    def test_trim_at_26(self, bot):
        """At 26 entries (after append), trims to keep last 25."""
        bot.manager_command_history = [{"seq": i} for i in range(25)]
        _append_command_history(bot, {"seq": 26})
        assert len(bot.manager_command_history) == 25
        assert bot.manager_command_history[-1]["seq"] == 26

    def test_trim_keeps_most_recent_25(self, bot):
        """After overflow, the oldest entry is dropped."""
        bot.manager_command_history = [{"seq": i} for i in range(25)]
        _append_command_history(bot, {"seq": 100})
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
        assert bot.manager_command_history[0]["status"] == "queued"
        assert bot.manager_command_history[1]["status"] == "done"
        assert bot.manager_command_history[2]["status"] == "queued"

    def test_seq_with_zero_fallback_matches_only_nonzero(self, bot):
        """row.get("seq") or 0 means missing/falsy seq treated as 0.
        mutmut_10 uses 'or 1' which treats missing seq as 1, corrupting matches."""
        bot.manager_command_history = [{"status": "original"}]
        _update_command_history(bot, 1, status="updated")
        assert bot.manager_command_history[0]["status"] == "original"

    def test_search_reversed_finds_latest_first(self, bot):
        """Search reversed: if multiple rows have same seq, updates most recent."""
        bot.manager_command_history = [
            {"seq": 5, "status": "old"},
            {"seq": 5, "status": "newer"},
        ]
        _update_command_history(bot, 5, status="latest")
        assert bot.manager_command_history[1]["status"] == "latest"
        assert bot.manager_command_history[0]["status"] == "old"

    def test_break_vs_continue_behavior(self, bot):
        """mutmut_12 uses break instead of continue.
        Test: seq=3 in first position should still be found with correct behavior."""
        bot.manager_command_history = [
            {"seq": 1, "status": "q"},
            {"seq": 2, "status": "q"},
            {"seq": 3, "status": "q"},
        ]
        _update_command_history(bot, 3, status="found")
        assert bot.manager_command_history[2]["status"] == "found"


# ---------------------------------------------------------------------------
# _queue_manager_command mutation killers
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
