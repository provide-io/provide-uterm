#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Coverage tests for process.py Windows-only branches.

Covers:
- lines 298-299: process=None on Windows → _taskkill_process_tree called
- branch 322->325 (False): os.name=='nt' → skip SIGKILL after SIGTERM timeout
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.terminal.manager.config import ManagerConfig
from provide.terminal.manager.core import SwarmManager
from provide.terminal.manager.process import BotProcessManager


class FakeWorkerPlugin:
    @property
    def worker_type(self) -> str:
        return "test_game"

    @property
    def worker_module(self) -> str:
        return "test_module"

    def configure_worker_env(self, env, bot_status, manager, **kwargs):
        pass


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
    return SwarmManager(config)


@pytest.fixture
def pm(manager, tmp_path):
    pm = BotProcessManager(
        manager,
        worker_registry={"test_game": FakeWorkerPlugin()},
        log_dir=str(tmp_path / "logs"),
    )
    manager.bot_process_manager = pm
    return pm


class TestStopProcessTreeWindowsPaths:
    """Cover process.py Windows-only branches: lines 298-299 and branch 322->325."""

    @pytest.mark.asyncio
    async def test_no_process_windows_calls_taskkill(self, pm):
        """Lines 298-299: process=None on Windows → _taskkill_process_tree called."""
        with (
            patch("provide.terminal.manager.process.os.name", "nt"),
            patch.object(
                BotProcessManager,
                "_taskkill_process_tree",
                new_callable=AsyncMock,
            ) as mock_taskkill,
        ):
            await pm._stop_process_tree(bot_id="bot_win", pid=12345, process=None)

        mock_taskkill.assert_awaited_once_with(12345)

    @pytest.mark.asyncio
    async def test_no_process_windows_suppresses_oserror(self, pm):
        """Lines 298-299: OSError from _taskkill_process_tree is suppressed (process=None)."""
        with (
            patch("provide.terminal.manager.process.os.name", "nt"),
            patch.object(
                BotProcessManager,
                "_taskkill_process_tree",
                new_callable=AsyncMock,
                side_effect=OSError("access denied"),
            ),
        ):
            # Should not raise
            await pm._stop_process_tree(bot_id="bot_win", pid=12345, process=None)

    @pytest.mark.asyncio
    async def test_sigkill_skipped_on_windows_after_sigterm_timeout(self, pm):
        """Branch 322->325 (False): os.name=='nt' → skip SIGKILL, go directly to second wait."""
        proc = MagicMock()
        proc.pid = 99997

        call_count = [0]

        async def fake_wait(p, timeout):
            call_count[0] += 1
            if call_count[0] == 1:
                raise TimeoutError("SIGTERM timeout")
            # second call succeeds

        with (
            patch("provide.terminal.manager.process.os.name", "nt"),
            patch.object(BotProcessManager, "_wait_for_process_exit", side_effect=fake_wait),
            patch.object(BotProcessManager, "_taskkill_process_tree", new_callable=AsyncMock) as mock_taskkill,
        ):
            await pm._stop_process_tree(bot_id="bot_win", process=proc, timeout_s=0.01)

        # taskkill called once (initial terminate), not SIGKILL path
        assert mock_taskkill.call_count >= 1
        # _wait_for_process_exit called twice (first timeout, then second succeeds)
        assert call_count[0] == 2
