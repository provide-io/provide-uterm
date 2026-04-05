#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.manager.models."""

from __future__ import annotations

import pytest

from provide.terminal.manager.models import (
    NO_STORE_HEADERS,
    AgentStatusBase,
    DashboardStaticFiles,
    SpawnBatchRequest,
    SwarmStatus,
)


class TestAgentStatusBase:
    def test_defaults(self):
        agent = AgentStatusBase(agent_id="agent_001")
        assert agent.agent_id == "agent_001"
        assert agent.state == "unknown"
        assert agent.pid is None
        assert agent.config is None
        assert agent.paused is False
        assert agent.pending_command_seq == 0
        assert agent.manager_command_history == []

    def test_all_fields(self):
        agent = AgentStatusBase(
            agent_id="b1",
            session_id="s1",
            state="running",
            pid=1234,
            config="/path.yaml",
            started_at=1000.0,
            stopped_at=2000.0,
            completed_at=3000.0,
            last_update_time=4000.0,
            error_message="oops",
            exit_reason="crash",
            is_hijacked=True,
            hijacked_by="admin",
            hijacked_at=5000.0,
            paused=True,
            respawned_from="agent_000",
            pending_command_seq=3,
            pending_command_type="set_goal",
            pending_command_payload={"goal": "trade"},
            manager_command_history=[{"seq": 1}],
        )
        assert agent.state == "running"
        assert agent.pid == 1234
        assert agent.is_hijacked is True
        assert agent.pending_command_payload == {"goal": "trade"}

    def test_model_dump(self):
        agent = AgentStatusBase(agent_id="agent_000")
        d = agent.model_dump()
        assert d["agent_id"] == "agent_000"
        assert "state" in d


class TestSwarmStatus:
    def test_basic(self):
        ss = SwarmStatus(
            total_agents=5,
            running=3,
            completed=1,
            errors=1,
            stopped=0,
            uptime_seconds=100.0,
            agents=[],
        )
        assert ss.total_agents == 5
        assert ss.swarm_paused is False
        assert ss.desired_agents == 0

    def test_extra_allow(self):
        """Game plugins can inject extra fields."""
        ss = SwarmStatus(
            total_agents=0,
            running=0,
            completed=0,
            errors=0,
            stopped=0,
            uptime_seconds=0,
            agents=[],
            total_credits=12345,  # extra field
            total_bank_credits=5000,  # extra field
        )
        d = ss.model_dump()
        assert d["total_credits"] == 12345
        assert d["total_bank_credits"] == 5000


class TestSpawnBatchRequest:
    def test_defaults(self):
        req = SpawnBatchRequest(config_paths=["/a.yaml"])
        assert req.group_size == 1
        assert req.group_delay == 12.0
        assert req.name_style == "random"
        assert req.name_base == ""

    def test_min_length(self):
        with pytest.raises(ValueError):
            SpawnBatchRequest(config_paths=[])


class TestDashboardStaticFiles:
    @pytest.mark.asyncio
    async def test_adds_no_store_headers_for_dashboard_js(self, tmp_path):
        js_file = tmp_path / "dashboard.js"
        js_file.write_text("// JS")
        static = DashboardStaticFiles(directory=str(tmp_path))

        # Build a mock scope matching a GET /static/dashboard.js request
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/static/dashboard.js",
            "root_path": "",
            "query_string": b"",
            "headers": [],
        }
        response = await static.get_response("dashboard.js", scope)
        for key, value in NO_STORE_HEADERS.items():
            assert response.headers.get(key) == value

    @pytest.mark.asyncio
    async def test_no_headers_for_other_files(self, tmp_path):
        css_file = tmp_path / "style.css"
        css_file.write_text("body{}")
        static = DashboardStaticFiles(directory=str(tmp_path))
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/static/style.css",
            "root_path": "",
            "query_string": b"",
            "headers": [],
        }
        response = await static.get_response("style.css", scope)
        assert "no-store" not in (response.headers.get("Cache-Control") or "")
