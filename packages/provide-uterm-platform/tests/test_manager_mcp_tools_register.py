#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""register_manager_tools() registers into a caller-supplied app."""

from __future__ import annotations

from typing import Any

import pytest

from provide.uterm.manager.mcp_tools import TOOL_COUNT, register_manager_tools


class _RecordingApp:
    """Minimal stand-in for a server app.

    Deliberately not a real MCPServer or FastMCP: register_manager_tools must
    work against either, so the test pins only the surface it is allowed to
    use — a `tool()` decorator factory.
    """

    def __init__(self) -> None:
        self.registered: list[str] = []

    def tool(self, *_args: Any, **_kwargs: Any) -> Any:
        def _decorator(fn: Any) -> Any:
            self.registered.append(fn.__name__)
            return fn

        return _decorator


class TestRegisterManagerTools:
    def test_registers_all_tools_into_supplied_app(self) -> None:
        app = _RecordingApp()
        count = register_manager_tools(app, base_url="http://test")
        assert count == TOOL_COUNT
        assert len(app.registered) == TOOL_COUNT

    def test_registers_swarm_status(self) -> None:
        app = _RecordingApp()
        register_manager_tools(app, base_url="http://test")
        assert "swarm_status" in app.registered

    def test_two_apps_are_independent(self) -> None:
        """The bbsbot per-app isolation case: no shared module-level registry."""
        first = _RecordingApp()
        second = _RecordingApp()
        register_manager_tools(first, base_url="http://one")
        register_manager_tools(second, base_url="http://two")
        assert first.registered == second.registered
        assert first.registered is not second.registered

    def test_requires_manager_or_base_url(self) -> None:
        app = _RecordingApp()
        with pytest.raises(ValueError, match="manager"):
            register_manager_tools(app)
