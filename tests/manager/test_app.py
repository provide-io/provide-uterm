# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for provide.terminal.manager.app factory."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi import APIRouter

from provide.terminal.manager.app import create_manager_app
from provide.terminal.manager.config import ManagerConfig
from provide.terminal.manager.core import SwarmManager
from provide.terminal.manager.models import BotStatusBase


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
        assert isinstance(manager, SwarmManager)
        assert manager.bot_process_manager is not None

    def test_custom_bot_status_class(self, config):
        class MyStatus(BotStatusBase):
            extra: str = "hi"

        app, manager = create_manager_app(config, bot_status_class=MyStatus)
        assert manager._bot_status_class is MyStatus

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

    def test_managed_bot_plugin(self, config):
        plugin = MagicMock()
        app, manager = create_manager_app(config, managed_bot=plugin)
        assert app.state.managed_bot_plugin is plugin

    def test_cors_from_env(self, config):
        with patch.dict(os.environ, {"UTERM_CORS_ORIGINS": "http://example.com"}):
            app, _manager = create_manager_app(config)
        # Verify app was created (CORS middleware applied internally)
        assert app is not None

    def test_cors_from_config(self, config):
        config.cors_origins = ["http://custom.example.com"]
        app, _manager = create_manager_app(config)
        assert app is not None

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
