#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.terminal.manager.config import ManagerConfig
from provide.terminal.manager.core import AgentManager
from provide.terminal.manager.ext import EVENT_AGENT_KILLED, EVENT_AGENT_SPAWNED
from provide.terminal.manager.process import AgentProcessManager


@pytest.fixture
def manager():
    config = ManagerConfig()
    mgr = AgentManager(config)
    pm = AgentProcessManager(mgr)
    mgr.agent_process_manager = pm
    return mgr


@pytest.mark.asyncio
async def test_spawn_policy_gate_allow(manager):
    """If policy gate allows, spawn proceeds."""
    pm = manager.agent_process_manager
    gate = AsyncMock()
    gate.intercept_spawn.return_value = True
    pm.set_policy_gate(gate)

    # Mock loading and process spawning
    with (
        patch.object(pm, "_load_worker_type", return_value=("shell", {})),
        patch.object(pm, "_get_registry_entry") as mock_reg,
        patch.object(pm, "_spawn_process", return_value=MagicMock(pid=1234)),
        patch("pathlib.Path.exists", return_value=True),
    ):
        mock_reg.return_value.worker_module = "fake_worker"

        agent_id = await pm.spawn_agent("fake_config.yaml", "agent_001")

        assert agent_id == "agent_001"
        gate.intercept_spawn.assert_awaited_once()


@pytest.mark.asyncio
async def test_spawn_policy_gate_reject(manager):
    """If policy gate rejects, spawn raises RuntimeError."""
    pm = manager.agent_process_manager
    gate = AsyncMock()
    gate.intercept_spawn.return_value = False
    pm.set_policy_gate(gate)

    with (
        patch.object(pm, "_load_worker_type", return_value=("shell", {})),
        patch("pathlib.Path.exists", return_value=True),
    ):
        with pytest.raises(RuntimeError, match="Spawn rejected by policy"):
            await pm.spawn_agent("fake_config.yaml", "agent_001")

        gate.intercept_spawn.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifecycle_telemetry(manager):
    """Standardized DAS events are emitted on spawn and kill."""
    pm = manager.agent_process_manager

    with (
        patch("provide.terminal.manager.process.logger") as mock_logger,
        patch.object(pm, "_load_worker_type", return_value=("shell", {})),
        patch.object(pm, "_get_registry_entry") as mock_reg,
        patch.object(pm, "_spawn_process", return_value=MagicMock(pid=1234)),
        patch.object(pm, "_stop_process_tree"),
        patch("pathlib.Path.exists", return_value=True),
    ):
        mock_reg.return_value.worker_module = "fake_worker"

        # Spawn
        await pm.spawn_agent("fake_config.yaml", "agent_001")
        mock_logger.info.assert_any_call(EVENT_AGENT_SPAWNED, agent_id="agent_001", pid=1234, worker_type="shell")

        # Kill
        await pm.kill_agent("agent_001")
        mock_logger.info.assert_any_call(EVENT_AGENT_KILLED, agent_id="agent_001")
