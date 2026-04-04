#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.manager.timeseries."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from provide.terminal.manager.models import SwarmStatus
from provide.terminal.manager.timeseries.manager import TimeseriesManager


def _make_status(**overrides):
    defaults = {
        "total_agents": 5,
        "running": 3,
        "completed": 1,
        "errors": 1,
        "stopped": 0,
        "uptime_seconds": 100.0,
        "agents": [],
    }
    defaults.update(overrides)
    return SwarmStatus(**defaults)


class TestTimeseriesManager:
    def test_init_creates_dir(self, tmp_path):
        ts_dir = tmp_path / "metrics"
        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(ts_dir))
        assert ts_dir.is_dir()
        assert mgr.samples_count == 0
        assert mgr.interval_s == 20

    def test_init_clamps_interval(self, tmp_path):
        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path), interval_s=0)
        assert mgr.interval_s == 1

    def test_get_info(self, tmp_path):
        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path))
        info = mgr.get_info()
        assert info["interval_seconds"] == 20
        assert info["samples"] == 0
        assert "path" in info

    def test_write_and_read(self, tmp_path):
        status = _make_status()
        mgr = TimeseriesManager(lambda: status, timeseries_dir=str(tmp_path))
        mgr.write_sample(status, reason="test")
        mgr.samples_count += 1
        rows = mgr.read_tail(10)
        assert len(rows) == 1
        assert rows[0]["reason"] == "test"
        assert rows[0]["total_agents"] == 5

    def test_read_tail_empty(self, tmp_path):
        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path))
        assert mgr.read_tail(10) == []

    def test_read_tail_nonexistent(self, tmp_path):
        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path))
        mgr.path = tmp_path / "nonexistent.jsonl"
        assert mgr.read_tail(10) == []

    def test_read_tail_empty_file(self, tmp_path):
        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path))
        mgr.path.write_text("")
        assert mgr.read_tail(10) == []

    def test_read_tail_corrupt_lines(self, tmp_path):
        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path))
        with mgr.path.open("w") as f:
            f.write("not json\n")
            f.write(json.dumps({"ts": 1, "total_agents": 1}) + "\n")
            f.write("[1,2,3]\n")  # array, not dict
        rows = mgr.read_tail(10)
        assert len(rows) == 1

    def test_read_tail_limits(self, tmp_path):
        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path))
        with mgr.path.open("w") as f:
            for i in range(50):
                f.write(json.dumps({"ts": i, "total_agents": 1, "total_turns": i}) + "\n")
        rows = mgr.read_tail(5)
        assert len(rows) == 5
        assert rows[-1]["ts"] == 49

    def test_get_recent(self, tmp_path):
        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path))
        status = _make_status()
        for _ in range(3):
            mgr.write_sample(status, reason="test")
        rows = mgr.get_recent(10)
        assert len(rows) == 3

    def test_get_summary_no_plugin(self, tmp_path):
        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path))
        result = mgr.get_summary(60)
        assert result["error"] == "no timeseries plugin configured"

    def test_get_summary_with_plugin(self, tmp_path):
        plugin = MagicMock()
        plugin.get_summary.return_value = {"window_minutes": 60, "rows": 10}
        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path), plugin=plugin)
        result = mgr.get_summary(60)
        assert result["rows"] == 10
        plugin.get_summary.assert_called_once_with(mgr, 60)

    def test_build_row_no_plugin(self, tmp_path):
        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path))
        status = _make_status()
        row = mgr._build_row(status, "test")
        assert row["reason"] == "test"
        assert row["total_agents"] == 5
        assert "ts" in row

    def test_build_row_with_plugin(self, tmp_path):
        plugin = MagicMock()
        plugin.build_row.return_value = {"custom": True}
        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path), plugin=plugin)
        row = mgr._build_row(_make_status(), "test")
        assert row == {"custom": True}

    def test_write_sample_handles_error(self, tmp_path):
        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path))
        # Make path read-only dir to cause write error
        mgr.path = tmp_path / "readonly" / "file.jsonl"
        # This should not raise
        mgr.write_sample(_make_status(), reason="test")
        # File handle should be reset after error
        assert mgr._fh is None

    def test_write_sample_reuses_file_handle(self, tmp_path):
        """write_sample keeps file handle open across calls."""
        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path))
        mgr.write_sample(_make_status(), reason="first")
        fh1 = mgr._fh
        assert fh1 is not None and not fh1.closed
        mgr.write_sample(_make_status(), reason="second")
        assert mgr._fh is fh1  # same handle reused
        rows = mgr.read_tail(10)
        assert len(rows) == 2

    def test_close_fh_closes_handle(self, tmp_path):
        """_close_fh closes the persistent handle."""
        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path))
        mgr.write_sample(_make_status(), reason="test")
        assert mgr._fh is not None
        mgr._close_fh()
        assert mgr._fh is None

    def test_rotate_closes_and_reopens_handle(self, tmp_path):
        """Rotation closes the old handle; next write opens a new one."""
        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path))
        mgr._max_bytes = 1  # force rotation on next write
        mgr.write_sample(_make_status(), reason="trigger-rotate")
        # Rotation closed the handle
        assert mgr._fh is None or mgr._fh.closed
        # Reset max_bytes so next write doesn't rotate again
        mgr._max_bytes = 10_000_000
        mgr.write_sample(_make_status(), reason="after-rotate")
        assert mgr._fh is not None and not mgr._fh.closed
        rows = mgr.read_tail(10)
        assert len(rows) == 1  # only the post-rotation sample in new file

    @pytest.mark.asyncio
    async def test_loop_writes_startup(self, tmp_path):
        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path), interval_s=1)
        # Run loop for a tiny bit then cancel
        import asyncio

        task = asyncio.create_task(mgr.loop())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert mgr.samples_count >= 1
        rows = mgr.read_tail(10)
        assert any(r.get("reason") == "startup" for r in rows)


class TestTrimToLatestEpoch:
    def test_empty(self):
        assert TimeseriesManager.trim_to_latest_epoch([]) == []

    def test_single(self):
        rows = [{"total_turns": 100, "total_agents": 5}]
        assert TimeseriesManager.trim_to_latest_epoch(rows) == rows

    def test_no_epoch_boundary(self):
        rows = [
            {"total_turns": 100, "total_agents": 5},
            {"total_turns": 110, "total_agents": 5},
            {"total_turns": 120, "total_agents": 5},
        ]
        assert TimeseriesManager.trim_to_latest_epoch(rows) == rows

    def test_turn_drop_triggers_epoch(self):
        rows = [
            {"total_turns": 1000, "total_agents": 5},
            {"total_turns": 1050, "total_agents": 5},
            {"total_turns": 100, "total_agents": 5},  # big drop
            {"total_turns": 150, "total_agents": 5},
        ]
        result = TimeseriesManager.trim_to_latest_epoch(rows)
        assert len(result) == 2
        assert result[0]["total_turns"] == 100

    def test_agents_to_zero_triggers_epoch(self):
        rows = [
            {"total_turns": 100, "total_agents": 5},
            {"total_turns": 0, "total_agents": 0},
            {"total_turns": 10, "total_agents": 3},
        ]
        result = TimeseriesManager.trim_to_latest_epoch(rows)
        # Epoch boundary at index 1 (agents dropped to 0), so rows[1:] returned
        assert len(result) == 2
        assert result[0]["total_agents"] == 0
        assert result[1]["total_turns"] == 10
