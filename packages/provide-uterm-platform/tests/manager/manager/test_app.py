#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.manager.app factory."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi import APIRouter

from provide.uterm.manager.app import create_manager_app
from provide.uterm.manager.config import ManagerConfig
from provide.uterm.manager.core import AgentManager
from provide.uterm.manager.models import AgentStatusBase


@pytest.fixture
def config(tmp_path):
    return ManagerConfig(
        state_file=str(tmp_path / "state.json"),
        timeseries_dir=str(tmp_path / "metrics"),
        log_dir=str(tmp_path / "logs"),
    )


class TestCreateManagerApp:
    def test_returns_app_and_manager(self, config):
        app, manager = create_manager_app(config)
        assert app is not None
        assert isinstance(manager, AgentManager)
        assert manager.agent_process_manager is not None

    def test_custom_agent_status_class(self, config):
        class MyStatus(AgentStatusBase):
            extra: str = "hi"

        app, manager = create_manager_app(config, agent_status_class=MyStatus)
        assert manager._agent_status_class is MyStatus

    def test_extra_routers(self, config):
        extra = APIRouter()

        @extra.get("/custom")
        async def custom():
            return {"custom": True}

        app, manager = create_manager_app(config, extra_routers=[extra])
        from fastapi.testclient import TestClient

        client = TestClient(app)
        resp = client.get("/custom")
        assert resp.status_code == 200
        assert resp.json()["custom"] is True

    def test_managed_agent_plugin(self, config):
        plugin = MagicMock()
        app, manager = create_manager_app(config, managed_agent=plugin)
        assert app.state.managed_agent_plugin is plugin

    def test_cors_from_env(self, config):
        with patch.dict(os.environ, {"UTERM_CORS_ORIGINS": "http://example.com"}):
            app, _manager = create_manager_app(config)
        # Verify app was created (CORS middleware applied internally)
        assert app is not None

    def test_cors_from_config(self, config):
        config.cors_origins = ["http://custom.example.com"]
        app, _manager = create_manager_app(config)
        assert app is not None

    def test_rejects_wildcard_origin_with_credentials(self, config):
        """A `*` origin with credentials is a CSRF foot-gun → refuse at build."""
        with (
            patch.dict(os.environ, {"UTERM_CORS_ORIGINS": "*"}),
            pytest.raises(ValueError, match="wildcard"),
        ):
            create_manager_app(config)

    def test_rejects_wildcard_origin_from_config(self, config):
        config.cors_origins = ["*"]
        with pytest.raises(ValueError, match="wildcard"):
            create_manager_app(config)

    def test_rejects_empty_origin_list(self, config):
        """Empty origins would let CORSMiddleware behave permissively — refuse."""
        config.cors_origins = []
        with pytest.raises(ValueError, match="origin"):
            create_manager_app(config)

    def test_plugins_wired(self, config):
        pool = MagicMock()
        identity = MagicMock()
        status_update = MagicMock()
        ts_plugin = MagicMock()

        app, manager = create_manager_app(
            config,
            account_pool=pool,
            identity_store=identity,
            status_update=status_update,
            timeseries=ts_plugin,
        )
        assert manager.account_pool is pool
        assert manager.identity_store is identity
        assert manager._status_update_plugin is status_update


class TestMCPClientEndpoint:
    @pytest.mark.asyncio
    async def test_mcp_client_registration(self, config):
        """Test that MCP client connection is registered on connect."""
        from fastapi.testclient import TestClient

        app, manager = create_manager_app(config)

        # Create a test client and open WebSocket
        client = TestClient(app)
        with client.websocket_connect("/ws/mcp-client") as _:
            # Verify client is registered
            assert len(manager.mcp_clients) == 1

    @pytest.mark.asyncio
    async def test_mcp_client_unregistration(self, config):
        """Test that MCP client is unregistered on disconnect."""
        from fastapi.testclient import TestClient

        app, manager = create_manager_app(config)

        client = TestClient(app)
        with client.websocket_connect("/ws/mcp-client") as _:
            assert len(manager.mcp_clients) == 1

        # After context exit, client should be unregistered
        assert len(manager.mcp_clients) == 0

    @pytest.mark.asyncio
    async def test_multiple_mcp_clients(self, config):
        """Test that multiple MCP clients can be registered simultaneously."""
        from fastapi.testclient import TestClient

        app, manager = create_manager_app(config)

        client = TestClient(app)
        with client.websocket_connect("/ws/mcp-client") as _:
            assert len(manager.mcp_clients) == 1
            with client.websocket_connect("/ws/mcp-client") as _:
                assert len(manager.mcp_clients) == 2

        assert len(manager.mcp_clients) == 0

    @pytest.mark.asyncio
    async def test_mcp_client_cancels_auto_shutdown(self, config):
        """Test that connecting an MCP client cancels pending auto-shutdown."""
        from fastapi.testclient import TestClient

        # Create manager
        app, manager = create_manager_app(config)

        client = TestClient(app)
        # Simulate a shutdown task being scheduled
        import asyncio

        shutdown_task = asyncio.create_task(asyncio.sleep(10))
        manager._mcp_shutdown_task = shutdown_task

        # Connect an MCP client
        with client.websocket_connect("/ws/mcp-client") as _:
            # Shutdown task should be cancelled and cleared
            assert manager._mcp_shutdown_task is None


class TestAutoShutdown:
    def test_check_auto_shutdown_disabled(self, config):
        """Test that auto-shutdown is skipped when disabled."""
        config.auto_shutdown_enabled = False
        app, manager = create_manager_app(config)

        import asyncio

        asyncio.run(manager._check_auto_shutdown())
        assert manager._mcp_shutdown_task is None

    @pytest.mark.asyncio
    async def test_check_auto_shutdown_with_mcp_clients(self, config):
        """Test that auto-shutdown is skipped when MCP clients are connected."""
        config.auto_shutdown_enabled = True
        app, manager = create_manager_app(config)

        # Add a fake MCP client
        from unittest.mock import MagicMock

        fake_ws = MagicMock()
        manager.mcp_clients.add(fake_ws)

        await manager._check_auto_shutdown()
        assert manager._mcp_shutdown_task is None

    @pytest.mark.asyncio
    async def test_check_auto_shutdown_with_active_agents(self, config):
        """Test that auto-shutdown is deferred when active agents exist."""
        config.auto_shutdown_enabled = True
        app, manager = create_manager_app(config)

        # Add an active agent
        from provide.uterm.manager.models import AgentStatusBase

        manager.agents["agent-1"] = AgentStatusBase(agent_id="agent-1", state="running")

        await manager._check_auto_shutdown()
        assert manager._mcp_shutdown_task is None

    @pytest.mark.asyncio
    async def test_check_auto_shutdown_already_scheduled(self, config):
        """Test that auto-shutdown is not rescheduled if already pending."""
        config.auto_shutdown_enabled = True
        app, manager = create_manager_app(config)

        import asyncio

        # Pre-schedule a shutdown task
        existing_task = asyncio.create_task(asyncio.sleep(10))
        manager._mcp_shutdown_task = existing_task

        await manager._check_auto_shutdown()
        # Should not create a new task
        assert manager._mcp_shutdown_task is existing_task

    @pytest.mark.asyncio
    async def test_check_auto_shutdown_creates_task(self, config):
        """Test that auto-shutdown task is created when all conditions are met."""
        config.auto_shutdown_enabled = True
        config.auto_shutdown_grace_s = 0.1
        app, manager = create_manager_app(config)

        await manager._check_auto_shutdown()
        assert manager._mcp_shutdown_task is not None
        # Clean up task
        manager._mcp_shutdown_task.cancel()

    @pytest.mark.asyncio
    async def test_auto_shutdown_after_cancelled(self, config):
        """Test that auto-shutdown gracefully handles cancellation."""
        import asyncio
        import contextlib

        config.auto_shutdown_enabled = True
        app, manager = create_manager_app(config)

        # Create the shutdown task and cancel it before grace period ends
        task = asyncio.create_task(manager._auto_shutdown_after(1.0))
        await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # Server should not be marked for shutdown
        assert manager._server is None

    @pytest.mark.asyncio
    async def test_auto_shutdown_after_mcp_clients_appear(self, config):
        """Test that shutdown is aborted if MCP clients connect during grace period."""
        config.auto_shutdown_enabled = True
        config.auto_shutdown_grace_s = 0.1
        app, manager = create_manager_app(config)

        # Start shutdown, then add an MCP client before grace period ends
        import asyncio
        from unittest.mock import MagicMock

        task = asyncio.create_task(manager._auto_shutdown_after(0.2))
        await asyncio.sleep(0.05)
        manager.mcp_clients.add(MagicMock())
        await task
        # Server should not be marked for shutdown
        assert manager._server is None

    @pytest.mark.asyncio
    async def test_auto_shutdown_after_active_agents_appear(self, config):
        """Test that shutdown is aborted if active agents appear during grace period."""
        config.auto_shutdown_enabled = True
        config.auto_shutdown_grace_s = 0.1
        app, manager = create_manager_app(config)

        # Start shutdown, then add an active agent before grace period ends
        import asyncio

        from provide.uterm.manager.models import AgentStatusBase

        task = asyncio.create_task(manager._auto_shutdown_after(0.2))
        await asyncio.sleep(0.05)
        manager.agents["agent-1"] = AgentStatusBase(agent_id="agent-1", state="running")
        await task
        # Server should not be marked for shutdown
        assert manager._server is None

    @pytest.mark.asyncio
    async def test_auto_shutdown_after_executes(self, config):
        """Test that shutdown executes when all conditions remain unmet."""
        config.auto_shutdown_enabled = True
        config.auto_shutdown_grace_s = 0.05
        app, manager = create_manager_app(config)

        # Create a minimal server object
        class FakeServer:
            def __init__(self):
                self.should_exit = False

        manager._server = FakeServer()

        await manager._auto_shutdown_after(0.05)
        # Server should be marked for shutdown
        assert manager._server.should_exit is True
