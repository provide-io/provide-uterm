#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Executable pin on the MCP 2.0 server API this package depends on.

This file exists to fail loudly when the SDK moves under us. The previous
generation of this guard (``test_fastmcp_contract.py`` in octowright) was
written for the same purpose and did not catch the 1.x -> 2.0 break, because
it pinned behaviour rather than the specific call shapes the code uses. Each
assertion below corresponds to a line of production code that would break.
"""

from __future__ import annotations

import inspect
from typing import Any

from mcp.server.mcpserver import Context, MCPServer, authenticated_principal
from mcp.server.mcpserver.tools.base import Tool


class TestServerConstruction:
    def test_mcpserver_accepts_name_and_lifespan(self) -> None:
        """create_mcp_app() passes both: MCPServer("uterm", lifespan=...)."""
        params = inspect.signature(MCPServer.__init__).parameters
        assert "name" in params
        assert "lifespan" in params

    def test_run_takes_transport_keyword(self) -> None:
        """cli.py calls app.run(transport="stdio") and must keep working."""
        params = inspect.signature(MCPServer.run).parameters
        assert "transport" in params

    async def test_list_tools_is_async(self) -> None:
        """Tool-count tests await mcp.list_tools()."""
        assert inspect.iscoroutinefunction(MCPServer.list_tools)


class TestToolDecoration:
    def test_tool_decorator_keywords(self) -> None:
        """@mcp.tool() is used bare, but these keywords must remain available."""
        params = inspect.signature(MCPServer.tool).parameters
        for expected in ("name", "title", "description", "annotations", "structured_output"):
            assert expected in params, f"MCPServer.tool() lost keyword {expected!r}"

    def test_optional_context_parameter_is_injected_not_exposed(self) -> None:
        """All 28 uterm tools are declared `ctx: Context | None = None`.

        The SDK must (a) recognise that as the injected context parameter and
        (b) keep it out of the client-facing input schema. If this regresses,
        `ctx` becomes a required client argument on every tool at once.
        """

        async def sample(session_id: str, ctx: Context | None = None) -> dict[str, Any]:
            """Sample."""
            return {}

        tool = Tool.from_function(sample)
        assert tool.context_kwarg == "ctx"
        assert list(tool.parameters["properties"]) == ["session_id"]


class TestAuthorizationPrimitives:
    def test_authenticated_principal_takes_request_context(self) -> None:
        """auth.resolve_principal() calls this with ctx.request_context."""
        params = inspect.signature(authenticated_principal).parameters
        assert list(params) == ["ctx"]

    def test_context_has_no_get_state(self) -> None:
        """The fastmcp state API this package used to depend on is gone.

        Pinned deliberately: if a future SDK adds `get_state` back, the
        contextvar-free workaround in auth.py should be revisited rather than
        left in place by inertia.
        """
        assert not hasattr(Context, "get_state")

    def test_request_context_raises_when_unbound(self) -> None:
        """auth.py must guard this access — it raises rather than returning None."""
        ctx: Any = Context(request_context=None, mcp_server=None)
        try:
            ctx.request_context  # noqa: B018
        except Exception:
            return
        raise AssertionError("expected Context.request_context to raise when unbound")
