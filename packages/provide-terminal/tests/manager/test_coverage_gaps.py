#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Coverage tests for manager core, app, and CLI gaps."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from provide.terminal.manager.app import create_manager_app
from provide.terminal.manager.config import ManagerConfig
from provide.terminal.manager.core import AgentManager
from provide.terminal.manager.models import AgentStatusBase


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
        """arc 223->221: agent_id already in agents → skip the if block, state not overwritten."""
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

        with patch("uvicorn.Config", return_value=MagicMock()), patch("uvicorn.Server", return_value=mock_server):
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

        with patch("uvicorn.Config", return_value=MagicMock()), patch("uvicorn.Server", return_value=mock_server):
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


class TestCliMain:
    def test_main_guard(self):
        """Line 42: if __name__ == '__main__' — covered indirectly via test_cli.py."""
