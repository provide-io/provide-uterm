#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for fan-out data models."""

from __future__ import annotations

from provide.terminal.bridge.fanout import FanOutGroup, FanOutResult, SessionFanOutResult


class TestFanOutGroupDefaults:
    def test_required_fields_stored(self) -> None:
        g = FanOutGroup(
            group_id="g1",
            name="My Group",
            worker_ids=["w1", "w2"],
            created_by="user1",
            created_at=1000.0,
        )
        assert g.group_id == "g1"
        assert g.name == "My Group"
        assert g.worker_ids == ["w1", "w2"]
        assert g.created_by == "user1"
        assert g.created_at == 1000.0

    def test_default_mode_is_parallel(self) -> None:
        g = FanOutGroup(group_id="g2", name="G", worker_ids=[], created_by="u", created_at=0.0)
        assert g.mode == "parallel"

    def test_default_stop_on_first_error_false(self) -> None:
        g = FanOutGroup(group_id="g3", name="G", worker_ids=[], created_by="u", created_at=0.0)
        assert g.stop_on_first_error is False

    def test_default_error_pattern_none(self) -> None:
        g = FanOutGroup(group_id="g4", name="G", worker_ids=[], created_by="u", created_at=0.0)
        assert g.error_pattern is None

    def test_default_quiesce_ms(self) -> None:
        g = FanOutGroup(group_id="g5", name="G", worker_ids=[], created_by="u", created_at=0.0)
        assert g.quiesce_ms == 500

    def test_default_max_response_ms(self) -> None:
        g = FanOutGroup(group_id="g6", name="G", worker_ids=[], created_by="u", created_at=0.0)
        assert g.max_response_ms == 10_000

    def test_default_divergence_threshold(self) -> None:
        g = FanOutGroup(group_id="g7", name="G", worker_ids=[], created_by="u", created_at=0.0)
        assert g.divergence_threshold == 0.8

    def test_default_grants_empty_list(self) -> None:
        g = FanOutGroup(group_id="g8", name="G", worker_ids=[], created_by="u", created_at=0.0)
        assert g.grants == []

    def test_grants_not_shared_between_instances(self) -> None:
        g1 = FanOutGroup(group_id="ga", name="G", worker_ids=[], created_by="u", created_at=0.0)
        g2 = FanOutGroup(group_id="gb", name="G", worker_ids=[], created_by="u", created_at=0.0)
        g1.grants.append("role:admin")
        assert g2.grants == []

    def test_override_defaults(self) -> None:
        g = FanOutGroup(
            group_id="g9",
            name="G",
            worker_ids=["w1"],
            created_by="u",
            created_at=42.0,
            mode="sequential",
            stop_on_first_error=True,
            error_pattern="ERROR",
            quiesce_ms=200,
            max_response_ms=5_000,
            divergence_threshold=0.5,
            grants=["role:viewer"],
        )
        assert g.mode == "sequential"
        assert g.stop_on_first_error is True
        assert g.error_pattern == "ERROR"
        assert g.quiesce_ms == 200
        assert g.max_response_ms == 5_000
        assert g.divergence_threshold == 0.5
        assert g.grants == ["role:viewer"]


class TestSessionFanOutResult:
    def test_field_access_ok(self) -> None:
        r = SessionFanOutResult(
            worker_id="w1",
            ok=True,
            output_delta="hello",
            elapsed_ms=42,
            divergent=False,
        )
        assert r.worker_id == "w1"
        assert r.ok is True
        assert r.output_delta == "hello"
        assert r.elapsed_ms == 42
        assert r.divergent is False

    def test_field_access_failed(self) -> None:
        r = SessionFanOutResult(
            worker_id="w2",
            ok=False,
            output_delta=None,
            elapsed_ms=9999,
            divergent=True,
        )
        assert r.ok is False
        assert r.output_delta is None
        assert r.divergent is True

    def test_slots_prevent_arbitrary_attributes(self) -> None:
        r = SessionFanOutResult(worker_id="w", ok=True, output_delta=None, elapsed_ms=1, divergent=False)
        try:
            r.extra = "nope"  # type: ignore[attr-defined]
            raise AssertionError("Should have raised AttributeError")  # pragma: no cover
        except AttributeError:
            pass


class TestFanOutResult:
    def test_aggregation_fields(self) -> None:
        results = [
            SessionFanOutResult(worker_id="w1", ok=True, output_delta="ok", elapsed_ms=10, divergent=False),
            SessionFanOutResult(worker_id="w2", ok=False, output_delta=None, elapsed_ms=200, divergent=False),
        ]
        fr = FanOutResult(
            group_id="g1",
            send_id="send-abc",
            command="ls",
            sent_at=1000.0,
            results=results,
            divergent_sessions=[],
            failed_sessions=["w2"],
        )
        assert fr.group_id == "g1"
        assert fr.send_id == "send-abc"
        assert fr.command == "ls"
        assert fr.sent_at == 1000.0
        assert len(fr.results) == 2
        assert fr.divergent_sessions == []
        assert fr.failed_sessions == ["w2"]

    def test_divergent_sessions_populated(self) -> None:
        fr = FanOutResult(
            group_id="g2",
            send_id="send-xyz",
            command="echo hi",
            sent_at=2000.0,
            results=[],
            divergent_sessions=["w3", "w4"],
            failed_sessions=[],
        )
        assert fr.divergent_sessions == ["w3", "w4"]
        assert fr.failed_sessions == []

    def test_results_list_contains_session_results(self) -> None:
        session_result = SessionFanOutResult(worker_id="w5", ok=True, output_delta="data", elapsed_ms=5, divergent=True)
        fr = FanOutResult(
            group_id="g3",
            send_id="s1",
            command="pwd",
            sent_at=3000.0,
            results=[session_result],
            divergent_sessions=["w5"],
            failed_sessions=[],
        )
        assert fr.results[0].worker_id == "w5"
        assert fr.results[0].divergent is True
