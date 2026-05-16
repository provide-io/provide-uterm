#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Coverage tests for manager/timeseries — read_tail, loop, and edge cases."""

from __future__ import annotations

import asyncio
import json

import pytest

from provide.uterm.manager.models import SwarmStatus
from provide.uterm.manager.timeseries.manager import TimeseriesManager


def _make_ts_manager(tmp_path, **kwargs) -> TimeseriesManager:
    return TimeseriesManager(
        lambda: SwarmStatus(total_agents=0, running=0, completed=0, errors=0, stopped=0, uptime_seconds=0, agents=[]),
        timeseries_dir=str(tmp_path),
        **kwargs,
    )


class TestTimeseriesManagerGaps:
    """Cover timeseries/manager.py lines 99, 102-103, 109, 169-170."""

    def test_read_tail_large_file_chunking(self, tmp_path):
        """Line 99: break in chunk read loop."""
        mgr = _make_ts_manager(tmp_path)
        with mgr.path.open("w") as f:
            for i in range(200):
                f.write(json.dumps({"ts": i, "total_agents": 1, "total_turns": i}) + "\n")
        rows = mgr.read_tail(10)
        assert len(rows) == 10
        assert rows[-1]["ts"] == 199

    def test_read_tail_io_error(self, tmp_path):
        """Lines 102-103: exception during file read."""
        mgr = _make_ts_manager(tmp_path)
        mgr.path.write_text("valid\n")
        mgr.path.chmod(0o000)
        try:
            rows = mgr.read_tail(10)
            assert rows == []
        finally:
            mgr.path.chmod(0o644)

    def test_read_tail_skips_blank_lines(self, tmp_path):
        """Line 109: continue on blank/non-dict lines."""
        mgr = _make_ts_manager(tmp_path)
        with mgr.path.open("w") as f:
            f.write("\n")
            f.write(json.dumps({"ts": 1}) + "\n")
            f.write("\n")
        rows = mgr.read_tail(10)
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_loop_writes_interval(self, tmp_path):
        """Lines 169-170: interval sample write in loop."""
        mgr = _make_ts_manager(tmp_path, interval_s=1)
        task = asyncio.create_task(mgr.loop())
        await asyncio.sleep(1.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert mgr.samples_count >= 2
        rows = mgr.read_tail(10)
        reasons = [r.get("reason") for r in rows]
        assert "startup" in reasons
        assert "interval" in reasons
