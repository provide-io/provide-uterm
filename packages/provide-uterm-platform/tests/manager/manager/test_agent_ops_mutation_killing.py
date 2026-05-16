#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for manager/routes/agent_ops.py helper functions (part 1).

Classes: TestCommandHistoryRowsMutationKilling, TestAppendCommandHistoryMutationKilling,
         TestUpdateCommandHistoryMutationKilling, TestQueueManagerCommandMutationKilling.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from provide.uterm.manager.app import create_manager_app
from provide.uterm.manager.config import ManagerConfig
from provide.uterm.manager.models import AgentStatusBase
from provide.uterm.manager.routes.agent_ops import (
    _append_command_history,
    _command_history_rows,
    _queue_manager_command,
    _update_command_history,
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
# _command_history_rows mutation killers
# ---------------------------------------------------------------------------


class TestCommandHistoryRowsMutationKilling:
    def test_returns_empty_list_when_no_attribute(self):
        """When manager_command_history is not set on an ad-hoc object, returns [].
        (mutmut_6: missing None default — getattr with no default raises AttributeError
        when attribute is absent; with None default it returns None → non-list → [].)"""
        import types

        obj = types.SimpleNamespace(agent_id="x")
        assert not hasattr(obj, "manager_command_history")
        rows = _command_history_rows(obj)  # type: ignore[arg-type]
        assert rows == []
        assert obj.manager_command_history == []

    def test_returns_existing_list(self, agent):
        """When manager_command_history is already a list, returns it."""
        agent.manager_command_history = [{"seq": 1}]
        rows = _command_history_rows(agent)
        assert rows == [{"seq": 1}]

    def test_replaces_non_list_with_empty_list(self, agent):
        """When manager_command_history is not a list, replaces with []."""
        agent.manager_command_history = "not a list"  # type: ignore[assignment]
        rows = _command_history_rows(agent)
        assert rows == []
        assert agent.manager_command_history == []  # type: ignore[comparison-overlap]


# ---------------------------------------------------------------------------
# _append_command_history mutation killers
# mutmut_5: >= 25 instead of > 25 (should trim at 26 entries not 25)
# ---------------------------------------------------------------------------


class TestAppendCommandHistoryMutationKilling:
    def test_no_trim_at_exactly_25(self, agent):
        """At exactly 25 entries, no trim occurs (mutmut_5: >= would trim at 25)."""
        agent.manager_command_history = [{"seq": i} for i in range(24)]
        _append_command_history(agent, {"seq": 25})
        assert len(agent.manager_command_history) == 25

    def test_trim_at_26(self, agent):
        """At 26 entries (after append), trims to keep last 25."""
        agent.manager_command_history = [{"seq": i} for i in range(25)]
        _append_command_history(agent, {"seq": 26})
        assert len(agent.manager_command_history) == 25
        assert agent.manager_command_history[-1]["seq"] == 26

    def test_trim_keeps_most_recent_25(self, agent):
        """After overflow, the oldest entry is dropped."""
        agent.manager_command_history = [{"seq": i} for i in range(25)]
        _append_command_history(agent, {"seq": 100})
        seqs = [r["seq"] for r in agent.manager_command_history]
        assert 0 not in seqs
        assert 100 in seqs

    def test_entry_is_copied_not_referenced(self, agent):
        """Entry is stored as a copy (dict(entry))."""
        entry = {"seq": 1, "extra": "data"}
        _append_command_history(agent, entry)
        entry["extra"] = "modified"
        assert agent.manager_command_history[0]["extra"] == "data"


# ---------------------------------------------------------------------------
# _update_command_history mutation killers
# mutmut_1: seq < 0 instead of seq <= 0 (seq=0 should NOT update)
# mutmut_2: seq <= 1 instead of seq <= 0 (seq=1 should update but mutant skips it)
# mutmut_10: row.get("seq") or 1 instead of or 0 (changes zero-seq fallback)
# mutmut_12: break instead of continue (stops iteration after first non-match)
# ---------------------------------------------------------------------------


class TestUpdateCommandHistoryMutationKilling:
    def test_seq_zero_does_not_update(self, agent):
        """seq=0 should do nothing (return early). mutmut_1 (seq<0) would update seq=0."""
        agent.manager_command_history = [{"seq": 0, "status": "original"}]
        _update_command_history(agent, 0, status="changed")
        assert agent.manager_command_history[0]["status"] == "original"

    def test_seq_one_updates(self, agent):
        """seq=1 should update the matching row. mutmut_2 (<=1) would skip seq=1."""
        agent.manager_command_history = [{"seq": 1, "status": "queued"}]
        _update_command_history(agent, 1, status="done")
        assert agent.manager_command_history[0]["status"] == "done"

    def test_updates_correct_row_when_multiple(self, agent):
        """Only the row with matching seq is updated (mutmut_12: break would stop early)."""
        agent.manager_command_history = [
            {"seq": 1, "status": "queued"},
            {"seq": 2, "status": "queued"},
            {"seq": 3, "status": "queued"},
        ]
        _update_command_history(agent, 2, status="done")
        assert agent.manager_command_history[0]["status"] == "queued"
        assert agent.manager_command_history[1]["status"] == "done"
        assert agent.manager_command_history[2]["status"] == "queued"

    def test_seq_with_zero_fallback_matches_only_nonzero(self, agent):
        """row.get("seq") or 0 means missing/falsy seq treated as 0.
        mutmut_10 uses 'or 1' which treats missing seq as 1, corrupting matches."""
        agent.manager_command_history = [{"status": "original"}]
        _update_command_history(agent, 1, status="updated")
        assert agent.manager_command_history[0]["status"] == "original"

    def test_search_reversed_finds_latest_first(self, agent):
        """Search reversed: if multiple rows have same seq, updates most recent."""
        agent.manager_command_history = [
            {"seq": 5, "status": "old"},
            {"seq": 5, "status": "newer"},
        ]
        _update_command_history(agent, 5, status="latest")
        assert agent.manager_command_history[1]["status"] == "latest"
        assert agent.manager_command_history[0]["status"] == "old"

    def test_break_vs_continue_behavior(self, agent):
        """mutmut_12 uses break instead of continue.
        Test: seq=3 in first position should still be found with correct behavior."""
        agent.manager_command_history = [
            {"seq": 1, "status": "q"},
            {"seq": 2, "status": "q"},
            {"seq": 3, "status": "q"},
        ]
        _update_command_history(agent, 3, status="found")
        assert agent.manager_command_history[2]["status"] == "found"


# ---------------------------------------------------------------------------
# _queue_manager_command mutation killers
# ---------------------------------------------------------------------------


class TestQueueManagerCommandMutationKilling:
    def test_first_command_gets_seq_1(self, agent):
        """First command has seq=1 (replaces 0+1)."""
        result = _queue_manager_command(agent, "set_goal", {"goal": "explore"})
        assert result["seq"] == 1
        assert agent.pending_command_seq == 1

    def test_second_command_gets_seq_2(self, agent):
        """Second command gets seq=2."""
        _queue_manager_command(agent, "set_goal", {"goal": "explore"})
        result = _queue_manager_command(agent, "set_goal", {"goal": "fight"})
        assert result["seq"] == 2
        assert agent.pending_command_seq == 2

    def test_type_stored(self, agent):
        """pending_command_type is set correctly."""
        _queue_manager_command(agent, "set_directive", {"directive": "be cautious"})
        assert agent.pending_command_type == "set_directive"

    def test_payload_copied(self, agent):
        """payload is stored as copy."""
        payload = {"goal": "test"}
        _queue_manager_command(agent, "set_goal", payload)
        payload["goal"] = "modified"
        assert agent.pending_command_payload["goal"] == "test"

    def test_replaces_field_none_for_first(self, agent):
        """First command has replaces=None (no previous seq)."""
        result = _queue_manager_command(agent, "set_goal", {"goal": "x"})
        assert result["replaces"] is None

    def test_replaces_field_set_for_second(self, agent):
        """Second command has replaces=1 (replaced the first)."""
        _queue_manager_command(agent, "set_goal", {"goal": "x"})
        result = _queue_manager_command(agent, "set_goal", {"goal": "y"})
        assert result["replaces"] == 1

    def test_history_entry_added(self, agent):
        """Command history entry added with status='queued'."""
        _queue_manager_command(agent, "set_goal", {"goal": "x"})
        rows = _command_history_rows(agent)
        assert len(rows) == 1
        assert rows[0]["status"] == "queued"
        assert rows[0]["seq"] == 1

    def test_replaced_command_marked_in_history(self, agent):
        """When replacing, previous command is updated to status='replaced'."""
        _queue_manager_command(agent, "set_goal", {"goal": "x"})
        _queue_manager_command(agent, "set_goal", {"goal": "y"})
        rows = _command_history_rows(agent)
        first = next(r for r in rows if r["seq"] == 1)
        assert first["status"] == "replaced"

    def test_result_contains_type(self, agent):
        """Result dict contains 'type' field."""
        result = _queue_manager_command(agent, "set_goal", {"goal": "z"})
        assert result["type"] == "set_goal"

    def test_result_contains_payload(self, agent):
        """Result dict contains 'payload' field."""
        result = _queue_manager_command(agent, "set_goal", {"goal": "z"})
        assert result["payload"] == {"goal": "z"}
