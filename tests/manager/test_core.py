#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.manager.core."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.terminal.manager.config import ManagerConfig
from provide.terminal.manager.core import AgentManager
from provide.terminal.manager.models import AgentStatusBase


@pytest.fixture
def config(tmp_path):
    return ManagerConfig(
        state_file=str(tmp_path / "state.json"),
        timeseries_dir=str(tmp_path / "metrics"),
    )


@pytest.fixture
def manager(config):
    mgr = AgentManager(config)
    # Provide a dummy process manager to avoid NoneType errors
    pm = MagicMock()
    pm.cancel_spawn = AsyncMock(return_value=False)
    pm.start_spawn_swarm = AsyncMock()
    pm.spawn_agent = AsyncMock(return_value="agent_000")
    pm.spawn_swarm = AsyncMock(return_value=["agent_000"])
    pm.kill_agent = AsyncMock()
    pm.monitor_processes = AsyncMock()
    mgr.agent_process_manager = pm
    return mgr


class TestAgentManagerInit:
    def test_defaults(self, config):
        mgr = AgentManager(config)
        assert mgr.max_agents == 200
        assert mgr.desired_agents == 0
        assert mgr.swarm_paused is False
        assert mgr.bust_respawn is False
        assert mgr.agents == {}
        assert mgr.processes == {}

    def test_custom_agent_status_class(self, config):
        class CustomStatus(AgentStatusBase):
            custom_field: str = "hello"

        mgr = AgentManager(config, agent_status_class=CustomStatus)
        assert mgr._agent_status_class is CustomStatus


class TestSwarmStatus:
    def test_default_builder(self, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.agents["agent_001"] = AgentStatusBase(agent_id="agent_001", state="completed")
        manager.agents["agent_002"] = AgentStatusBase(agent_id="agent_002", state="error")
        status = manager.get_swarm_status()
        assert status.total_agents == 3
        assert status.running == 1
        assert status.completed == 1
        assert status.errors == 1

    def test_custom_builder(self, config):
        def custom_builder(mgr):
            from provide.terminal.manager.models import SwarmStatus

            return SwarmStatus(
                total_agents=42,
                running=42,
                completed=0,
                errors=0,
                stopped=0,
                uptime_seconds=0,
                agents=[],
                total_credits=1000,
            )

        mgr = AgentManager(config, swarm_status_builder=custom_builder)
        mgr.agent_process_manager = MagicMock()
        status = mgr.get_swarm_status()
        assert status.total_agents == 42
        d = status.model_dump()
        assert d["total_credits"] == 1000


class TestBroadcastStatus:
    @pytest.mark.asyncio
    async def test_broadcast_sends_to_clients(self, manager):
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        manager.websocket_clients = {ws1, ws2}
        await manager.broadcast_status()
        ws1.send_text.assert_awaited_once()
        ws2.send_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_broadcast_removes_disconnected(self, manager):
        ws_good = AsyncMock()
        ws_bad = AsyncMock()
        ws_bad.send_text.side_effect = ConnectionError("gone")
        manager.websocket_clients = {ws_good, ws_bad}
        await manager.broadcast_status()
        assert ws_bad not in manager.websocket_clients
        assert ws_good in manager.websocket_clients


class TestStatePersistence:
    def test_write_and_load(self, manager, tmp_path):
        state = {
            "timestamp": 1000.0,
            "desired_agents": 5,
            "swarm_paused": True,
            "bust_respawn": True,
            "agents": {
                "agent_000": {"agent_id": "agent_000", "state": "stopped"},
            },
        }
        manager._write_state(state)
        # Reset and reload
        manager.desired_agents = 0
        manager.swarm_paused = False
        manager.bust_respawn = False
        manager.agents.clear()
        manager._load_state()
        assert manager.desired_agents == 5
        assert manager.swarm_paused is True
        assert manager.bust_respawn is True
        assert "agent_000" in manager.agents

    def test_load_running_becomes_stopped(self, manager, tmp_path):
        state = {
            "agents": {"agent_000": {"agent_id": "agent_000", "state": "running"}},
        }
        manager._write_state(state)
        manager._load_state()
        assert manager.agents["agent_000"].state == "stopped"

    def test_load_no_file(self, config, tmp_path):
        config.state_file = str(tmp_path / "nonexistent.json")
        mgr = AgentManager(config)
        mgr.agent_process_manager = MagicMock()
        mgr._load_state()  # should not raise
        assert mgr.desired_agents == 0

    def test_load_empty_state_file(self, manager):
        manager.state_file = ""
        manager._load_state()  # should not raise

    def test_load_corrupt_json(self, manager, tmp_path):
        state_path = tmp_path / "state.json"
        state_path.write_text("not json")
        manager.state_file = str(state_path)
        manager._load_state()  # should not raise

    def test_load_skips_invalid_agent(self, manager, tmp_path):
        state = {
            "agents": {"bad": {"not_valid": True}},
        }
        manager._write_state(state)
        manager._load_state()  # should log warning, not crash

    def test_write_state_handles_error(self, manager, tmp_path):
        manager.state_file = str(tmp_path / "readonly" / "nested" / "state.json")
        # Parent dir doesn't exist - should not raise
        manager._write_state({"test": True})


class TestDelegation:
    @pytest.mark.asyncio
    async def test_cancel_spawn(self, manager):
        await manager.cancel_spawn()
        manager.agent_process_manager.cancel_spawn.assert_awaited()

    @pytest.mark.asyncio
    async def test_start_spawn_swarm(self, manager):
        await manager.start_spawn_swarm(["/a.yaml"])
        manager.agent_process_manager.start_spawn_swarm.assert_awaited()

    @pytest.mark.asyncio
    async def test_spawn_agent(self, manager):
        result = await manager.spawn_agent("/a.yaml", "agent_000")
        assert result == "agent_000"

    @pytest.mark.asyncio
    async def test_spawn_swarm(self, manager):
        result = await manager.spawn_swarm(["/a.yaml"])
        assert result == ["agent_000"]

    @pytest.mark.asyncio
    async def test_kill_agent(self, manager):
        await manager.kill_agent("agent_000")
        manager.agent_process_manager.kill_agent.assert_awaited_with("agent_000")

    def test_timeseries_info(self, manager):
        info = manager.get_timeseries_info()
        assert "path" in info

    def test_timeseries_recent(self, manager):
        rows = manager.get_timeseries_recent()
        assert isinstance(rows, list)

    def test_timeseries_summary(self, manager):
        result = manager.get_timeseries_summary()
        assert isinstance(result, dict)
