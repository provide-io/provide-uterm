#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests verifying that the session_annotate MCP tool is registered in the MCP app."""

from __future__ import annotations

from provide.uterm.ai.server import create_mcp_app


async def test_session_annotate_tool_exists() -> None:
    app = create_mcp_app("http://localhost:8780")
    tool_names = [t.name for t in await app.list_tools()]
    assert "session_annotate" in tool_names
