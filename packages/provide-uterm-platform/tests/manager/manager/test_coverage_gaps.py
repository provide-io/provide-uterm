#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Coverage tests for manager core, app, and CLI gaps."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from provide.uterm.manager.app import create_manager_app
from provide.uterm.manager.config import ManagerConfig
from provide.uterm.manager.core import AgentManager
from provide.uterm.manager.models import AgentStatusBase


def _make_status() -> SimpleNamespace:
    return SimpleNamespace(
        total_agents=5,
        running=3,
        completed=1,
        errors=0,
        stopped=1,
        uptime_seconds=100.0,
    )


@pytest.fixture
def config(tmp_path):
    return ManagerConfig(
        state_file=str(tmp_path / "state.json"),
        timeseries_dir=str(tmp_path / "metrics"),
        log_dir=str(tmp_path / "logs"),
        health_check_interval_s=0,
        heartbeat_timeout_s=1,
    )


@pytest.fixture
def manager(config):
    return AgentManager(config)


class TestCoreLoadStateSkipsBadAgent:
    """Cover lines 232-233: agent_state_load_skipped warning."""

    def test_load_state_skips_agent_with_bad_data(self, manager, tmp_path):
        state = {
            "agents": {
                "agent_bad": {"agent_id": 12345},  # agent_id must be str
            },
        }
        manager._write_state(state)
        manager._load_state()
        assert "agent_bad" not in manager.agents

    def test_load_state_skips_already_known_agent(self, manager, tmp_path):
        """arc 223->221: known agent_id skips load, not overwritten."""
        manager.agents["agent_known"] = AgentStatusBase(agent_id="agent_known", state="running")
        state = {
            "agents": {
                "agent_known": {"agent_id": "agent_known", "state": "stopped"},
            },
        }
        manager._write_state(state)
        manager._load_state()
        assert manager.agents["agent_known"].state == "running"


class TestCoreRunMethod:
    """Cover lines 248-287: the run() method."""

    @pytest.mark.asyncio
    async def test_run_starts_and_stops(self, config, tmp_path):
        config.state_file = str(tmp_path / "state.json")
        mgr = AgentManager(config)
        pm_mock = MagicMock()
        pm_mock.monitor_processes = AsyncMock()
        mgr.agent_process_manager = pm_mock
        mgr.timeseries_manager.loop = AsyncMock()

        mock_server = AsyncMock()
        mock_server.serve = AsyncMock()

        with (
            patch("uvicorn.Config", return_value=MagicMock()),
            patch("uvicorn.Server", return_value=mock_server),
        ):
            await mgr.run()
            mock_server.serve.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_shuts_down_hub(self, config, tmp_path):
        config.state_file = str(tmp_path / "state.json")
        mgr = AgentManager(config)
        pm_mock = MagicMock()
        pm_mock.monitor_processes = AsyncMock()
        mgr.agent_process_manager = pm_mock
        mgr.timeseries_manager.loop = AsyncMock()

        mock_hub = AsyncMock()
        mgr.term_hub = mock_hub

        mock_server = AsyncMock()
        mock_server.serve = AsyncMock()

        with (
            patch("uvicorn.Config", return_value=MagicMock()),
            patch("uvicorn.Server", return_value=mock_server),
        ):
            await mgr.run()

        mock_hub.shutdown.assert_awaited_once()


class TestAppWebSocketError:
    """Cover app.py lines 111-114: websocket error handler."""

    def test_websocket_error_cleanup(self, tmp_path):
        config = ManagerConfig(
            state_file=str(tmp_path / "s.json"),
            timeseries_dir=str(tmp_path / "m"),
        )
        app, manager = create_manager_app(config)
        client = TestClient(app)
        with client.websocket_connect("/ws/swarm") as ws:
            ws.send_text("ping")


class TestCleanupOldWorkerLogs:
    def test_deletes_stale_prev_and_orphan_logs(self, tmp_path: Path) -> None:
        """_cleanup_old_worker_logs deletes old .prev and orphan .log files."""
        import os

        from provide.uterm.manager._monitor import _cleanup_old_worker_logs

        log_dir = tmp_path / "logs" / "workers"
        log_dir.mkdir(parents=True)
        # Create a stale .prev file
        stale_prev = log_dir / "agent_0.log.prev"
        stale_prev.write_text("old")
        os.utime(stale_prev, (0, 0))  # very old mtime
        # Create a stale orphan log (agent not active)
        orphan_log = log_dir / "agent_99.log"
        orphan_log.write_text("orphan")
        os.utime(orphan_log, (0, 0))
        # Create a recent file (should NOT be deleted)
        recent = log_dir / "agent_1.log"
        recent.write_text("recent")

        pm = MagicMock()
        pm._log_dir = str(log_dir)
        pm.manager.agents = {"agent_1": MagicMock()}
        deleted = _cleanup_old_worker_logs(pm)
        assert deleted == 2
        assert not stale_prev.exists()
        assert not orphan_log.exists()
        assert recent.exists()

    def test_no_log_dir_returns_zero(self, tmp_path: Path) -> None:
        pm = MagicMock()
        pm._log_dir = str(tmp_path / "nonexistent")
        pm.manager.agents = {}
        from provide.uterm.manager._monitor import _cleanup_old_worker_logs

        assert _cleanup_old_worker_logs(pm) == 0

    def test_subdirectory_skipped(self, tmp_path: Path) -> None:
        """Line 96: continue when path is not a file (subdirectory)."""
        import os

        from provide.uterm.manager._monitor import _cleanup_old_worker_logs

        log_dir = tmp_path / "logs" / "workers"
        log_dir.mkdir(parents=True)
        # Create a subdirectory — not a file → line 96 continue
        (log_dir / "subdir").mkdir()
        # Create a stale orphan log so we exercise the rest of the loop too
        orphan = log_dir / "agent_99.log"
        orphan.write_text("orphan")
        os.utime(orphan, (0, 0))

        pm = MagicMock()
        pm._log_dir = str(log_dir)
        pm.manager.agents = {}
        deleted = _cleanup_old_worker_logs(pm)
        assert deleted == 1
        assert (log_dir / "subdir").is_dir()

    def test_stat_oserror_continues(self, tmp_path: Path) -> None:
        """Lines 99-100: OSError from stat() → continue."""
        from provide.uterm.manager._monitor import _cleanup_old_worker_logs

        log_dir = tmp_path / "logs" / "workers"
        log_dir.mkdir(parents=True)
        bad_file = log_dir / "agent_bad.log"
        bad_file.write_text("data")

        pm = MagicMock()
        pm._log_dir = str(log_dir)
        pm.manager.agents = {}

        orig_stat = Path.stat
        call_count: dict[str, int] = {}

        def broken_stat(self_path, *a, **kw):
            if self_path.name == "agent_bad.log":
                count = call_count.get("agent_bad.log", 0)
                call_count["agent_bad.log"] = count + 1
                # First call is from is_file(); second is the explicit stat()
                if count >= 1:
                    raise OSError("stat failed")
            return orig_stat(self_path, *a, **kw)

        with patch.object(Path, "stat", broken_stat):
            deleted = _cleanup_old_worker_logs(pm)
        assert deleted == 0

    def test_recent_prev_not_deleted(self, tmp_path: Path) -> None:
        """Line 103->94: recent .prev file loops back without deletion."""
        from provide.uterm.manager._monitor import _cleanup_old_worker_logs

        log_dir = tmp_path / "logs" / "workers"
        log_dir.mkdir(parents=True)
        # Create a recent .prev file — mtime is now, so mtime >= cutoff
        recent_prev = log_dir / "agent_0.log.prev"
        recent_prev.write_text("recent prev")

        pm = MagicMock()
        pm._log_dir = str(log_dir)
        pm.manager.agents = {}
        deleted = _cleanup_old_worker_logs(pm)
        assert deleted == 0
        assert recent_prev.exists()

    def test_old_active_agent_log_not_deleted(self, tmp_path: Path) -> None:
        """Line 103->94: old active agent .log not deleted."""
        import os

        from provide.uterm.manager._monitor import _cleanup_old_worker_logs

        log_dir = tmp_path / "logs" / "workers"
        log_dir.mkdir(parents=True)
        # Old log file but agent is active → stem in active_ids → not deleted
        active_log = log_dir / "agent_1.log"
        active_log.write_text("active agent log")
        os.utime(active_log, (0, 0))  # old mtime

        pm = MagicMock()
        pm._log_dir = str(log_dir)
        pm.manager.agents = {"agent_1": MagicMock()}
        deleted = _cleanup_old_worker_logs(pm)
        assert deleted == 0
        assert active_log.exists()

    def test_no_stale_files_returns_zero(self, tmp_path: Path) -> None:
        """Line 106->108: deleted==0 branch — no cleanup log emitted."""
        from provide.uterm.manager._monitor import _cleanup_old_worker_logs

        log_dir = tmp_path / "logs" / "workers"
        log_dir.mkdir(parents=True)
        # Only recent files, all belonging to active agents
        recent = log_dir / "agent_1.log"
        recent.write_text("recent")

        pm = MagicMock()
        pm._log_dir = str(log_dir)
        pm.manager.agents = {"agent_1": MagicMock()}
        deleted = _cleanup_old_worker_logs(pm)
        assert deleted == 0


class TestLogRotation:
    def test_oversized_log_rotated_via_spawn_process(self, tmp_path: Path) -> None:
        """process.py:277-281 — _spawn_process rotates oversized log to .prev."""
        import subprocess

        from provide.uterm.manager.constants import WORKER_LOG_MAX_BYTES
        from provide.uterm.manager.process import AgentProcessManager

        log_dir = tmp_path / "logs" / "workers"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "agent_0.log"
        log_file.write_bytes(b"x" * (WORKER_LOG_MAX_BYTES + 1))

        mgr_mock = MagicMock()
        pm = AgentProcessManager.__new__(AgentProcessManager)
        pm.manager = mgr_mock
        pm._log_dir = str(log_dir)

        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 12345

        with patch("subprocess.Popen", return_value=mock_proc):
            pm._spawn_process("agent_0", ["echo", "test"], {})

        # The old oversized log should have been rotated to .prev
        assert (log_dir / "agent_0.log.prev").exists()
        # A new log file was opened (created by _spawn_process)
        assert (log_dir / "agent_0.log").exists()

    def test_small_log_not_rotated(self, tmp_path: Path) -> None:
        """process.py:278->282 — existing log that is NOT oversized is not rotated."""
        import subprocess

        from provide.uterm.manager.process import AgentProcessManager

        log_dir = tmp_path / "logs" / "workers"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "agent_0.log"
        log_file.write_text("small")  # well under WORKER_LOG_MAX_BYTES

        mgr_mock = MagicMock()
        pm = AgentProcessManager.__new__(AgentProcessManager)
        pm.manager = mgr_mock
        pm._log_dir = str(log_dir)

        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 12345

        with patch("subprocess.Popen", return_value=mock_proc):
            pm._spawn_process("agent_0", ["echo", "test"], {})

        # No .prev file should be created
        assert not (log_dir / "agent_0.log.prev").exists()
        # The log file is still there (overwritten by open("w"))
        assert log_file.exists()


class TestTimeseriesCleanup:
    def test_cleanup_old_deletes_stale_files(self, tmp_path: Path) -> None:
        """timeseries/manager.py:168-175 — _cleanup_old deletes old files."""
        import os

        from provide.uterm.manager.timeseries.manager import TimeseriesManager

        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path))
        # Create an old timeseries file
        old_file = tmp_path / "swarm_timeseries_20200101_000000.jsonl"
        old_file.write_text("{}")
        os.utime(old_file, (0, 0))
        # Run cleanup
        mgr._cleanup_old(1.0)
        assert not old_file.exists()

    def test_rotate_oserror_returns_early(self, tmp_path: Path) -> None:
        """timeseries/manager.py:194-195 — OSError in stat returns early."""
        from provide.uterm.manager.timeseries.manager import TimeseriesManager

        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path))
        # Point path to nonexistent file — stat() raises OSError
        mgr.path = tmp_path / "nonexistent.jsonl"
        mgr._rotate_if_needed()  # should not raise

    def test_cleanup_skips_current_path(self, tmp_path: Path) -> None:
        """Line 169: if f == self.path: continue — skip the active file."""
        import os

        from provide.uterm.manager.timeseries.manager import TimeseriesManager

        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path))
        # Make the current path file old so it would be deleted if not skipped
        mgr.path.write_text("{}")
        os.utime(mgr.path, (0, 0))
        mgr._cleanup_old(1.0)
        # Current path must survive
        assert mgr.path.exists()

    def test_cleanup_skips_recent_file(self, tmp_path: Path) -> None:
        """Line 171->167: recent file (mtime >= cutoff) is not deleted."""
        from provide.uterm.manager.timeseries.manager import TimeseriesManager

        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path))
        # Create a recent timeseries file — default mtime is now
        recent = tmp_path / "swarm_timeseries_20990101_000000.jsonl"
        recent.write_text("{}")
        mgr._cleanup_old(1.0)
        assert recent.exists()

    def test_cleanup_oserror_on_stat(self, tmp_path: Path) -> None:
        """Lines 174-175: OSError during stat() → continue."""
        import os

        from provide.uterm.manager.timeseries.manager import TimeseriesManager

        mgr = TimeseriesManager(lambda: _make_status(), timeseries_dir=str(tmp_path))
        bad_file = tmp_path / "swarm_timeseries_20200101_000000.jsonl"
        bad_file.write_text("{}")
        os.utime(bad_file, (0, 0))

        orig_stat = Path.stat

        def broken_stat(self_path, *a, **kw):
            if self_path.name == bad_file.name:
                raise OSError("stat failed")
            return orig_stat(self_path, *a, **kw)

        with patch.object(Path, "stat", broken_stat):
            mgr._cleanup_old(1.0)
        # File still exists because stat() failed → continue
        assert bad_file.exists()


class TestSubreaperBranchMiss:
    def test_subreaper_prctl_returns_nonzero(self) -> None:
        """process.py:108->exit — prctl returns nonzero (failure)."""
        from provide.uterm.manager.process import AgentProcessManager

        mock_libc = MagicMock()
        mock_libc.prctl.return_value = -1  # failure
        with (
            patch("sys.platform", "linux"),
            patch("ctypes.CDLL", return_value=mock_libc),
        ):
            AgentProcessManager._try_set_subreaper()
        mock_libc.prctl.assert_called_once()


class TestCliMain:
    def test_main_guard(self):
        """Line 42: if __name__ == '__main__' — covered indirectly via test_cli.py."""
