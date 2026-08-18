#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.manager.app import create_manager_app
from provide.uterm.manager.config import ManagerConfig
from provide.uterm.manager.core import AgentManager
from provide.uterm.manager.ext import EVENT_AGENT_KILLED, EVENT_AGENT_SPAWNED, WebhookAgentSpawnPolicyGate
from provide.uterm.manager.process import AgentProcessManager


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

    # spawn_agent now logs via process_impl_spawn.logger (it was extracted there),
    # while kill_agent still logs via process_impl.logger. Patch BOTH module loggers
    # with the same mock so both spawn + kill events land on it.
    mock_logger = MagicMock()
    with (
        patch("provide.uterm.manager.process_impl.logger", mock_logger),
        patch("provide.uterm.manager.process_impl_spawn.logger", mock_logger),
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


def test_create_manager_app_installs_webhook_policy_gate(tmp_path):
    config = ManagerConfig(
        state_file=str(tmp_path / "state.json"),
        timeseries_dir=str(tmp_path / "metrics"),
        log_dir=str(tmp_path / "logs"),
        spawn_policy_webhook_url="https://policy.example/spawn",
        spawn_policy_webhook_secret="secret",
        spawn_policy_webhook_timeout_s=1.25,
    )

    _app, mgr = create_manager_app(config)

    gate = mgr.agent_process_manager._policy_gate
    assert isinstance(gate, WebhookAgentSpawnPolicyGate)
    assert gate.url == "https://policy.example/spawn"
    assert gate.secret == "secret"
    assert gate.timeout == 1.25


@pytest.mark.asyncio
async def test_webhook_policy_gate_allows_on_200_allow_true():
    gate = WebhookAgentSpawnPolicyGate("https://policy.example/spawn", timeout_s=0.5)
    response = MagicMock(status_code=200)
    response.json.return_value = {"allow": True}
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = response

    with patch("provide.uterm.manager.ext.httpx2.AsyncClient", return_value=client) as async_client:
        assert await gate.intercept_spawn("agent_123", "config.yaml", {"worker_type": "shell"}) is True

    async_client.assert_called_once_with(timeout=0.5)
    args, kwargs = client.post.call_args
    assert args == ("https://policy.example/spawn",)
    assert json.loads(kwargs["content"]) == {
        "agent_id": "agent_123",
        "config_path": "config.yaml",
        "raw_config": {"worker_type": "shell"},
    }
    # No secret on this gate → request is unsigned.
    assert "X-Signature" not in kwargs["headers"]


@pytest.mark.asyncio
async def test_webhook_policy_gate_signs_body_when_secret_set():
    """When a secret is configured the request body is HMAC-signed."""
    gate = WebhookAgentSpawnPolicyGate("https://policy.example/allow", secret="s3cret")
    response = MagicMock(status_code=200)
    response.json.return_value = {"allow": True}
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = response

    with patch("provide.uterm.manager.ext.httpx2.AsyncClient", return_value=client):
        assert await gate.intercept_spawn("a1", "/cfg/a.yaml", {"k": "v"}) is True

    _, kwargs = client.post.call_args
    body = kwargs["content"]
    sig = kwargs["headers"]["X-Signature"]
    expected = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(sig, expected)
    assert kwargs["headers"]["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_webhook_policy_gate_unsigned_when_no_secret():
    """Without a secret no X-Signature header is sent (body still posted)."""
    gate = WebhookAgentSpawnPolicyGate("https://policy.example/allow")
    response = MagicMock(status_code=200)
    response.json.return_value = {"allow": True}
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = response

    with patch("provide.uterm.manager.ext.httpx2.AsyncClient", return_value=client):
        assert await gate.intercept_spawn("a1", "/cfg/a.yaml", {"k": "v"}) is True

    _, kwargs = client.post.call_args
    assert "X-Signature" not in kwargs["headers"]


@pytest.mark.asyncio
async def test_webhook_policy_gate_rejects_non_200_or_exception():
    gate = WebhookAgentSpawnPolicyGate("https://policy.example/spawn")
    response = MagicMock(status_code=503)
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = response

    with patch("provide.uterm.manager.ext.httpx2.AsyncClient", return_value=client):
        assert await gate.intercept_spawn("agent_123", "config.yaml", {}) is False

    failing_client = AsyncMock()
    failing_client.__aenter__.side_effect = RuntimeError("network down")
    with patch("provide.uterm.manager.ext.httpx2.AsyncClient", return_value=failing_client):
        assert await gate.intercept_spawn("agent_123", "config.yaml", {}) is False
