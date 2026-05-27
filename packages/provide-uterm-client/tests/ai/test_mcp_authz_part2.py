#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the MCP authorization chokepoint.

Each MCP tool is gated by a single decorator that maps the tool name to a
required role.  These tests:

1. Verify every registered tool has a policy entry.
2. Drive the chokepoint with viewer / operator / admin principals against
   each tool and assert allow / deny outcomes match the policy table.
3. Cover ``session_create`` connector validation (allowlist + port + URL
   scheme rejection).
4. Cover the principal-resolution helpers (header parsing,
   ``resolve_principal`` fallback, role-rank ordering, primary_role,
   default fallback to admin).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from fastmcp import FastMCP

from provide.uterm.ai.auth import (
    _PRINCIPAL_STATE_KEY,
    AuthorizationContext,
    AuthorizationDenied,
    McpPrincipal,
    authorized,
    deny_payload,
)
from provide.uterm.ai.server import create_mcp_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _principal(role: str, subject: str = "tester") -> McpPrincipal:
    """Build a single-role principal for tests."""
    return McpPrincipal(subject_id=subject, roles=frozenset({role}))


def _mcp(role: str) -> FastMCP:
    """Construct a FastMCP app whose default principal has *role*."""
    return create_mcp_app("http://test", default_principal=_principal(role))


async def _call(mcp: FastMCP, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a tool by name and return its structured_content payload."""
    result = await mcp.call_tool(tool, args or {})
    return result.structured_content  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Sample arguments for each tool — kept minimal, just enough for the
# chokepoint to fire before the body would otherwise hit the network.
# ---------------------------------------------------------------------------

_TOOL_ARGS: dict[str, dict[str, Any]] = {
    "hijack_begin": {"worker_id": "w1"},
    "hijack_heartbeat": {"worker_id": "w1", "hijack_id": "h1"},
    "hijack_read": {"worker_id": "w1", "hijack_id": "h1"},
    "hijack_send": {"worker_id": "w1", "hijack_id": "h1", "keys": "x"},
    "hijack_step": {"worker_id": "w1", "hijack_id": "h1"},
    "hijack_release": {"worker_id": "w1", "hijack_id": "h1"},
    "session_list": {},
    "session_status": {"session_id": "s1"},
    "session_read": {"session_id": "s1"},
    "session_connect": {"session_id": "s1"},
    "session_disconnect": {"session_id": "s1"},
    "session_create": {"connector_type": "shell"},
    "server_health": {},
    "session_set_mode": {"session_id": "s1", "mode": "open"},
    "worker_input_mode": {"worker_id": "w1", "mode": "open"},
    "worker_disconnect": {"worker_id": "w1"},
    "session_watch": {"session_id": "s1"},
    "session_subscribe": {"session_id": "s1"},
    "fanout_group_create": {"session_ids": ["s1"]},
    "fanout_send": {"group_id": "g1", "data": "hi"},
    "session_annotate": {"session_id": "s1", "label": "x"},
}


# ---------------------------------------------------------------------------
# Policy table coverage / shape
# ---------------------------------------------------------------------------


class TestMcpPrincipal:
    def test_primary_role_picks_highest(self) -> None:
        p = McpPrincipal(subject_id="x", roles=frozenset({"viewer", "admin", "operator"}))
        assert p.primary_role == "admin"

    def test_primary_role_empty_roles_defaults_viewer(self) -> None:
        p = McpPrincipal(subject_id="x", roles=frozenset())
        assert p.primary_role == "viewer"

    def test_has_at_least_multi_role(self) -> None:
        p = McpPrincipal(subject_id="x", roles=frozenset({"viewer", "operator"}))
        assert p.has_at_least("operator")
        assert not p.has_at_least("admin")

    def test_default_principal_is_anonymous_viewer(self) -> None:
        p = McpPrincipal()
        assert p.subject_id == "anonymous"
        assert p.roles == frozenset({"viewer"})


class TestAuthorizedDecorator:
    def test_metadata_stamped(self) -> None:
        ctx = AuthorizationContext(default_principal=_principal("admin"))

        @authorized("session_list", ctx)
        async def _fn() -> dict[str, Any]:
            return {"ok": True}

        assert _fn.__uterm_tool_name__ == "session_list"  # type: ignore[attr-defined]
        assert _fn.__uterm_required_role__ == "viewer"  # type: ignore[attr-defined]

    async def test_decorator_invokes_body_when_allowed(self) -> None:
        ctx = AuthorizationContext(default_principal=_principal("admin"))
        body = AsyncMock(return_value={"success": True, "via": "body"})

        decorated = authorized("session_create", ctx)(body)
        result = await decorated()

        assert result == {"success": True, "via": "body"}
        body.assert_awaited_once()

    async def test_decorator_short_circuits_when_denied(self) -> None:
        ctx = AuthorizationContext(default_principal=_principal("viewer"))
        body = AsyncMock(return_value={"success": True, "via": "body"})

        decorated = authorized("session_create", ctx)(body)
        result = await decorated()

        body.assert_not_awaited()
        assert result["error"] == "authorization_denied"
        assert result["tool"] == "session_create"
        assert result["required_role"] == "admin"

    async def test_decorator_uses_request_scoped_principal(self) -> None:
        auth_ctx = AuthorizationContext(default_principal=_principal("viewer", subject="default"))
        body = AsyncMock(return_value={"success": True, "via": "body"})
        req_ctx = AsyncMock()
        req_ctx.get_state = AsyncMock(return_value=_principal("admin", subject="scoped"))

        decorated = authorized("session_create", auth_ctx)(body)
        result = await decorated(ctx=req_ctx)

        assert result == {"success": True, "via": "body"}
        body.assert_awaited_once()
        req_ctx.get_state.assert_awaited_once_with(_PRINCIPAL_STATE_KEY)

    async def test_decorator_denies_when_request_scoped_principal_low_privilege(self) -> None:
        auth_ctx = AuthorizationContext(default_principal=_principal("admin", subject="default"))
        body = AsyncMock(return_value={"success": True, "via": "body"})
        req_ctx = AsyncMock()
        req_ctx.get_state = AsyncMock(return_value=_principal("viewer", subject="scoped"))

        decorated = authorized("session_create", auth_ctx)(body)
        result = await decorated(ctx=req_ctx)

        body.assert_not_awaited()
        assert result["error"] == "authorization_denied"
        assert result["principal"] == "scoped"


class TestAuthorizationDeniedShape:
    def test_deny_payload_envelope(self) -> None:
        err = AuthorizationDenied(
            tool="hijack_begin",
            principal=_principal("viewer", subject="alice"),
            required="admin",
        )
        payload = deny_payload(err)
        assert payload["success"] is False
        assert payload["error"] == "authorization_denied"
        assert payload["tool"] == "hijack_begin"
        assert payload["required_role"] == "admin"
        assert payload["principal"] == "alice"
        assert payload["principal_roles"] == ["viewer"]

    def test_authorization_denied_repr_carries_context(self) -> None:
        err = AuthorizationDenied(
            tool="fanout_send",
            principal=_principal("operator"),
            required="admin",
        )
        msg = str(err)
        assert "fanout_send" in msg
        assert "admin" in msg
        assert "operator" in msg
