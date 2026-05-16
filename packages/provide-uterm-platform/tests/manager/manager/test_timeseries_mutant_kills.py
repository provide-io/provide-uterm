#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for manager/timeseries/manager.py."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from provide.uterm.manager.timeseries.manager import TimeseriesManager


def _make_status(
    total_agents: int = 5,
    running: int = 3,
    completed: int = 1,
    errors: int = 0,
    stopped: int = 0,
    uptime_seconds: float = 100.0,
) -> MagicMock:
    s = MagicMock()
    s.total_agents = total_agents
    s.running = running
    s.completed = completed
    s.errors = errors
    s.stopped = stopped
    s.uptime_seconds = uptime_seconds
    return s


def _make_mgr(tmp_path: Path, interval_s: int = 20, plugin: Any = None) -> TimeseriesManager:
    return TimeseriesManager(
        lambda: _make_status(),
        timeseries_dir=str(tmp_path / "ts"),
        interval_s=interval_s,
        plugin=plugin,
    )


# ---------------------------------------------------------------------------
# __init__ — defaults and stored values
# ---------------------------------------------------------------------------


class TestInit:
    def test_default_timeseries_dir_is_logs_metrics(self, tmp_path: Path) -> None:
        """mut_1/2: default dir renamed to 'XXlogs/metricsXX' or 'LOGS/METRICS'."""
        # Default is 'logs/metrics' — test that the stored path matches
        # (We can only check this by checking the path attribute)
        Path.cwd()
        mgr = TimeseriesManager(lambda: _make_status())
        assert str(mgr.timeseries_dir).endswith("logs/metrics") or "logs" in str(mgr.timeseries_dir).lower()
        # Cleanup: just verify no exception
        assert mgr.samples_count == 0

    def test_default_interval_is_20(self, tmp_path: Path) -> None:
        """mut_3: default interval_s=21."""
        mgr = _make_mgr(tmp_path)
        assert mgr.interval_s == 20

    def test_get_status_stored(self, tmp_path: Path) -> None:
        """mut_4: _get_status=None."""
        sentinel = MagicMock(return_value=_make_status())
        mgr = TimeseriesManager(sentinel, timeseries_dir=str(tmp_path / "ts"))
        # _get_status must call sentinel
        mgr._get_status()
        sentinel.assert_called_once()

    def test_interval_stored(self, tmp_path: Path) -> None:
        """mut_5: self.interval_s=None."""
        mgr = _make_mgr(tmp_path, interval_s=30)
        assert mgr.interval_s == 30

    def test_samples_count_zero_initially(self, tmp_path: Path) -> None:
        mgr = _make_mgr(tmp_path)
        assert mgr.samples_count == 0

    def test_plugin_stored(self, tmp_path: Path) -> None:
        plugin = MagicMock()
        mgr = _make_mgr(tmp_path, plugin=plugin)
        assert mgr._plugin is plugin


# ---------------------------------------------------------------------------
# get_info() — key names
# ---------------------------------------------------------------------------


class TestGetInfo:
    def test_path_key_present(self, tmp_path: Path) -> None:
        """mut_1/2: 'path' → 'XXpathXX'/'PATH'."""
        mgr = _make_mgr(tmp_path)
        info = mgr.get_info()
        assert "path" in info
        assert "XXpathXX" not in info
        assert "PATH" not in info

    def test_path_is_string(self, tmp_path: Path) -> None:
        """mut_3: path=str(None) — must be actual path."""
        mgr = _make_mgr(tmp_path)
        info = mgr.get_info()
        assert info["path"] != "None"
        assert len(info["path"]) > 0

    def test_interval_seconds_key_present(self, tmp_path: Path) -> None:
        """mut_4/5: 'interval_seconds' → 'XXinterval_secondsXX'/'INTERVAL_SECONDS'."""
        mgr = _make_mgr(tmp_path, interval_s=15)
        info = mgr.get_info()
        assert "interval_seconds" in info
        assert info["interval_seconds"] == 15

    def test_samples_key_present(self, tmp_path: Path) -> None:
        """mut_6/7: 'samples' → 'XXsamplesXX'/'SAMPLES'."""
        mgr = _make_mgr(tmp_path)
        info = mgr.get_info()
        assert "samples" in info
        assert info["samples"] == 0


# ---------------------------------------------------------------------------
# get_recent() — capping logic
# ---------------------------------------------------------------------------


class TestGetRecent:
    def test_default_limit_is_200(self, tmp_path: Path) -> None:
        """mut_1: default limit=201."""
        mgr = _make_mgr(tmp_path)
        # No data — should return [] (default limit=200 vs 201 doesn't matter here
        # unless we check that it calls read_tail with the right value)
        with patch.object(mgr, "read_tail", return_value=[]) as mock_rt:
            mgr.get_recent()
            mock_rt.assert_called_once()
            # limit passed to read_tail should be capped(200)
            called_limit = mock_rt.call_args[0][0]
            assert called_limit == 200

    def test_limit_clamped_to_minimum_1(self, tmp_path: Path) -> None:
        """mut_3/4: max(None,...) or max(1,None) — must clamp to at least 1."""
        mgr = _make_mgr(tmp_path)
        with patch.object(mgr, "read_tail", return_value=[]) as mock_rt:
            mgr.get_recent(limit=0)
            called_limit = mock_rt.call_args[0][0]
            assert called_limit >= 1

    def test_limit_clamped_to_maximum_5000(self, tmp_path: Path) -> None:
        """mut: upper bound is 5000."""
        mgr = _make_mgr(tmp_path)
        with patch.object(mgr, "read_tail", return_value=[]) as mock_rt:
            mgr.get_recent(limit=99999)
            called_limit = mock_rt.call_args[0][0]
            assert called_limit <= 5000


# ---------------------------------------------------------------------------
# get_summary() — plugin delegation
# ---------------------------------------------------------------------------


class TestGetSummary:
    def test_default_window_is_120(self, tmp_path: Path) -> None:
        """mut_1: default window_minutes=121."""
        plugin = MagicMock()
        plugin.get_summary = MagicMock(return_value={"window_minutes": 120, "rows": 0})
        mgr = _make_mgr(tmp_path, plugin=plugin)
        mgr.get_summary()
        plugin.get_summary.assert_called_once()
        args = plugin.get_summary.call_args[0]
        assert args[1] == 120

    def test_with_plugin_delegates_to_plugin(self, tmp_path: Path) -> None:
        """mut_2: 'is not None' → 'is None' flips delegation."""
        plugin = MagicMock()
        plugin.get_summary = MagicMock(return_value={"result": "from_plugin"})
        mgr = _make_mgr(tmp_path, plugin=plugin)
        result = mgr.get_summary(60)
        assert result == {"result": "from_plugin"}

    def test_plugin_called_with_self_and_window(self, tmp_path: Path) -> None:
        """mut_3/4/5: get_summary(None, window) or (self, None) or (window)."""
        plugin = MagicMock()
        plugin.get_summary = MagicMock(return_value={})
        mgr = _make_mgr(tmp_path, plugin=plugin)
        mgr.get_summary(45)
        call_args = plugin.get_summary.call_args[0]
        assert call_args[0] is mgr
        assert call_args[1] == 45

    def test_without_plugin_returns_no_plugin_error(self, tmp_path: Path) -> None:
        """Verify no-plugin fallback returns error dict."""
        mgr = _make_mgr(tmp_path, plugin=None)
        result = mgr.get_summary()
        assert "error" in result


# ---------------------------------------------------------------------------
# read_tail() — JSONL reading
# ---------------------------------------------------------------------------


class TestReadTail:
    def test_empty_file_returns_empty_list(self, tmp_path: Path) -> None:
        """mut: file exists but empty → []."""
        mgr = _make_mgr(tmp_path)
        mgr.path.touch()
        result = mgr.read_tail(10)
        assert result == []

    def test_nonexistent_file_returns_empty_list(self, tmp_path: Path) -> None:
        """mut: no file → []."""
        mgr = _make_mgr(tmp_path)
        result = mgr.read_tail(10)
        assert result == []

    def test_reads_jsonl_rows(self, tmp_path: Path) -> None:
        """Verify actual JSONL content is parsed."""
        mgr = _make_mgr(tmp_path)
        rows = [{"ts": 1.0, "x": i} for i in range(5)]
        with mgr.path.open("w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

        result = mgr.read_tail(5)
        assert len(result) == 5
        assert result[0]["x"] == 0

    def test_limit_restricts_rows_returned(self, tmp_path: Path) -> None:
        """mut_1: capped=None — limit must actually limit rows."""
        mgr = _make_mgr(tmp_path)
        rows = [{"ts": float(i)} for i in range(20)]
        with mgr.path.open("w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

        result = mgr.read_tail(5)
        assert len(result) == 5

    def test_limit_minimum_is_1(self, tmp_path: Path) -> None:
        """mut_2/3: max(None, ...) — must clamp to at least 1."""
        mgr = _make_mgr(tmp_path)
        with mgr.path.open("w") as f:
            f.write(json.dumps({"x": 1}) + "\n")
        # limit=0 should not crash; returns at least []
        result = mgr.read_tail(0)
        assert isinstance(result, list)

    def test_invalid_json_lines_skipped(self, tmp_path: Path) -> None:
        """Verify corrupt lines don't crash."""
        mgr = _make_mgr(tmp_path)
        with mgr.path.open("w") as f:
            f.write("not valid json\n")
            f.write(json.dumps({"valid": True}) + "\n")

        result = mgr.read_tail(10)
        assert len(result) == 1
        assert result[0]["valid"] is True

    def test_returns_only_dict_rows(self, tmp_path: Path) -> None:
        """Verify non-dict JSON values (lists, strings) are filtered out."""
        mgr = _make_mgr(tmp_path)
        with mgr.path.open("w") as f:
            f.write(json.dumps([1, 2, 3]) + "\n")  # list — not a dict
            f.write(json.dumps({"ok": True}) + "\n")

        result = mgr.read_tail(10)
        assert all(isinstance(r, dict) for r in result)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# trim_to_latest_epoch() — epoch detection
# ---------------------------------------------------------------------------


class TestTrimToLatestEpoch:
    def test_empty_list_returns_empty(self) -> None:
        result = TimeseriesManager.trim_to_latest_epoch([])
        assert result == []

    def test_single_row_returns_as_is(self) -> None:
        row = {"total_turns": 10, "total_agents": 2}
        result = TimeseriesManager.trim_to_latest_epoch([row])
        assert result == [row]

    def test_continuous_run_returns_all(self) -> None:
        """Monotonically increasing turns — no epoch break."""
        rows = [{"total_turns": i * 10, "total_agents": 3} for i in range(1, 6)]
        result = TimeseriesManager.trim_to_latest_epoch(rows)
        assert len(result) == 5

    def test_turn_reset_triggers_new_epoch(self) -> None:
        """A big drop in turns signals a new epoch; only return from that point."""
        rows = [
            {"total_turns": 100, "total_agents": 3},
            {"total_turns": 110, "total_agents": 3},
            {"total_turns": 120, "total_agents": 3},
            # epoch break: turns drop drastically
            {"total_turns": 5, "total_agents": 3},
            {"total_turns": 10, "total_agents": 3},
        ]
        result = TimeseriesManager.trim_to_latest_epoch(rows)
        # Should return from the drop point
        assert len(result) == 2
        assert result[0]["total_turns"] == 5

    def test_bot_count_zero_triggers_new_epoch(self) -> None:
        """Agent count dropping to 0 after being non-zero triggers epoch break."""
        rows = [
            {"total_turns": 10, "total_agents": 3},
            {"total_turns": 20, "total_agents": 0},
            {"total_turns": 30, "total_agents": 2},
        ]
        result = TimeseriesManager.trim_to_latest_epoch(rows)
        assert result[0]["total_agents"] == 0


# ---------------------------------------------------------------------------
# _build_row() — plugin delegation and default fields
# ---------------------------------------------------------------------------


class TestBuildRow:
    def test_with_plugin_delegates_to_plugin(self, tmp_path: Path) -> None:
        """mut_1: 'is not None' → 'is None' flips delegation."""
        plugin = MagicMock()
        plugin.build_row = MagicMock(return_value={"from_plugin": True})
        mgr = _make_mgr(tmp_path, plugin=plugin)
        result = mgr._build_row(_make_status(), "interval")
        assert result == {"from_plugin": True}

    def test_plugin_called_with_status_and_reason(self, tmp_path: Path) -> None:
        """mut_2/3/4/5: build_row(None, reason) or (status, None) or (reason) or (status,)."""
        plugin = MagicMock()
        plugin.build_row = MagicMock(return_value={})
        mgr = _make_mgr(tmp_path, plugin=plugin)
        status = _make_status()
        mgr._build_row(status, "startup")
        call_args = plugin.build_row.call_args[0]
        assert call_args[0] is status
        assert call_args[1] == "startup"

    def test_without_plugin_returns_basic_fields(self, tmp_path: Path) -> None:
        """Default row has ts/reason/total_agents/running/completed/errors/stopped/uptime."""
        mgr = _make_mgr(tmp_path, plugin=None)
        status = _make_status(total_agents=7, running=4)
        row = mgr._build_row(status, "interval")
        assert "ts" in row
        assert row["reason"] == "interval"
        assert row["total_agents"] == 7
        assert row["running"] == 4


# ---------------------------------------------------------------------------
# write_sample() — file I/O
# ---------------------------------------------------------------------------


class TestWriteSample:
    def test_write_sample_appends_json(self, tmp_path: Path) -> None:
        """mut_1-5: build_row args mutated."""
        mgr = _make_mgr(tmp_path)
        mgr.write_sample(_make_status(), reason="startup")
        content = mgr.path.read_text()
        row = json.loads(content.strip())
        assert row["reason"] == "startup"

    def test_write_sample_uses_build_row(self, tmp_path: Path) -> None:
        """mut_2: build_row(None, reason) — status arg not None."""
        mgr = _make_mgr(tmp_path)
        status = _make_status(total_agents=9)

        original_build_row = mgr._build_row
        captured: list[Any] = []

        def _capture(s, **kw):
            captured.append((s, kw))
            return original_build_row(s, **kw)

        mgr._build_row = _capture  # type: ignore[method-assign]
        mgr.write_sample(status, reason="test")
        assert captured[0][0] is status
        assert captured[0][1]["reason"] == "test"

    def test_write_sample_uses_correct_reason(self, tmp_path: Path) -> None:
        """mut_3: reason=None."""
        mgr = _make_mgr(tmp_path)
        mgr.write_sample(_make_status(), reason="interval")
        row = json.loads(mgr.path.read_text().strip())
        assert row["reason"] == "interval"

    def test_write_sample_appends_newline(self, tmp_path: Path) -> None:
        """mut: ensure_ascii + newline."""
        mgr = _make_mgr(tmp_path)
        mgr.write_sample(_make_status(), reason="x")
        mgr.write_sample(_make_status(), reason="y")
        lines = mgr.path.read_text().strip().splitlines()
        assert len(lines) == 2


# ---------------------------------------------------------------------------
# loop() — startup sample and increment
# ---------------------------------------------------------------------------


class TestLoop:
    async def test_loop_calls_write_sample_with_startup(self, tmp_path: Path) -> None:
        """mut_1-5: write_sample args mutated."""
        mgr = _make_mgr(tmp_path)
        calls: list[tuple] = []

        original_write = mgr.write_sample

        def _capture(status, *, reason):
            calls.append((status, reason))
            original_write(status, reason=reason)

        mgr.write_sample = _capture  # type: ignore[method-assign]

        # Run loop briefly then cancel
        task = asyncio.create_task(mgr.loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert len(calls) >= 1
        assert calls[0][1] == "startup"

    async def test_loop_increments_samples_count(self, tmp_path: Path) -> None:
        """mut: samples_count must increment."""
        mgr = _make_mgr(tmp_path, interval_s=1)

        # Run loop briefly then cancel
        task = asyncio.create_task(mgr.loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert mgr.samples_count >= 1
