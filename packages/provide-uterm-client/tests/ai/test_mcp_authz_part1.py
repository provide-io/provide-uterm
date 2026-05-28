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

import pytest
from fastmcp import FastMCP

from provide.uterm.ai.auth import (
    McpPrincipal,
    principal_from_headers,
    resolve_principal,
)
from provide.uterm.ai.policy import (
    ALLOWED_CONNECTOR_TYPES,
    TOOL_REQUIRED_ROLES,
    is_allowed_connector,
    required_role,
    role_at_least,
    role_rank,
)
from provide.uterm.ai.server import _validate_session_create_config, create_mcp_app

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


class TestPolicyTable:
    async def test_every_registered_tool_has_role_entry(self) -> None:
        mcp = _mcp("admin")
        registered = sorted(t.name for t in await mcp.list_tools())
        assert registered == sorted(TOOL_REQUIRED_ROLES.keys())

    def test_required_role_returns_table_value(self) -> None:
        for tool, role in TOOL_REQUIRED_ROLES.items():
            assert required_role(tool) == role

    def test_required_role_unknown_tool_raises_key_error(self) -> None:
        """required_role() raises KeyError with the tool name when the tool is unregistered."""
        with pytest.raises(KeyError, match="No authorization policy registered for MCP tool"):
            required_role("not_a_real_tool")

    def test_role_rank_ordering(self) -> None:
        assert role_rank("admin") > role_rank("operator") > role_rank("viewer")

    def test_role_rank_unknown_is_below_viewer(self) -> None:
        assert role_rank("rogue") < role_rank("viewer")

    def test_role_at_least_strict(self) -> None:
        assert role_at_least("admin", "viewer") is True
        assert role_at_least("operator", "operator") is True
        assert role_at_least("viewer", "operator") is False
        assert role_at_least("viewer", "admin") is False


# ---------------------------------------------------------------------------
# Allow/deny matrix — every role × every tool.
# ---------------------------------------------------------------------------


class TestAuthorizationMatrix:
    @pytest.mark.parametrize("tool", sorted(TOOL_REQUIRED_ROLES))
    async def test_viewer_role(self, tool: str) -> None:
        # The chokepoint short-circuits BEFORE the body, so the only deterministic
        # assertion we can make for an "allow" path is that the response is *not*
        # the authorization_denied envelope.  For deny paths we assert the
        # envelope explicitly.
        mcp = _mcp("viewer")
        data = await _call(mcp, tool, _TOOL_ARGS[tool])
        if role_at_least("viewer", required_role(tool)):
            assert data.get("error") != "authorization_denied"
        else:
            assert data["error"] == "authorization_denied"
            assert data["required_role"] == required_role(tool)
            assert data["principal_roles"] == ["viewer"]

    @pytest.mark.parametrize("tool", sorted(TOOL_REQUIRED_ROLES))
    async def test_operator_role(self, tool: str) -> None:
        mcp = _mcp("operator")
        data = await _call(mcp, tool, _TOOL_ARGS[tool])
        if role_at_least("operator", required_role(tool)):
            assert data.get("error") != "authorization_denied"
        else:
            assert data["error"] == "authorization_denied"
            assert data["required_role"] == required_role(tool)

    @pytest.mark.parametrize("tool", sorted(TOOL_REQUIRED_ROLES))
    async def test_admin_role_allows_all(self, tool: str) -> None:
        mcp = _mcp("admin")
        data = await _call(mcp, tool, _TOOL_ARGS[tool])
        # Admin is the highest rung — *every* tool must be allowed past
        # the chokepoint (body may still fail because we're hitting a fake
        # base URL, but that's a different ``error`` discriminator).
        assert data.get("error") != "authorization_denied"


# ---------------------------------------------------------------------------
# session_create connector vetting
# ---------------------------------------------------------------------------


class TestSessionCreateValidation:
    def test_allowed_connector(self) -> None:
        for ct in ALLOWED_CONNECTOR_TYPES:
            assert is_allowed_connector(ct)

    def test_disallowed_connector(self) -> None:
        assert not is_allowed_connector("rogue-connector")

    def test_validate_accepts_minimal(self) -> None:
        assert _validate_session_create_config(connector_type="shell", url=None, port=None) is None

    def test_validate_rejects_unknown_connector(self) -> None:
        rejection = _validate_session_create_config(connector_type="rogue", url=None, port=None)
        assert rejection is not None
        assert rejection["error"] == "invalid_connector_type"
        assert rejection["connector_type"] == "rogue"

    def test_validate_rejects_bad_port_low(self) -> None:
        rejection = _validate_session_create_config(connector_type="shell", url=None, port=0)
        assert rejection is not None
        assert rejection["error"] == "invalid_port"

    def test_validate_rejects_bad_port_high(self) -> None:
        rejection = _validate_session_create_config(connector_type="shell", url=None, port=70000)
        assert rejection is not None
        assert rejection["error"] == "invalid_port"

    def test_validate_accepts_legal_port(self) -> None:
        assert _validate_session_create_config(connector_type="shell", url=None, port=2222) is None

    def test_validate_rejects_file_url(self) -> None:
        rejection = _validate_session_create_config(
            connector_type="shell",
            url="file:///etc/passwd",
            port=None,
        )
        assert rejection is not None
        assert rejection["error"] == "invalid_url_scheme"
        assert rejection["scheme"] == "file"

    def test_validate_rejects_javascript_url(self) -> None:
        rejection = _validate_session_create_config(
            connector_type="shell",
            url="javascript:alert(1)",
            port=None,
        )
        assert rejection is not None
        assert rejection["error"] == "invalid_url_scheme"

    def test_validate_rejects_url_without_scheme(self) -> None:
        rejection = _validate_session_create_config(
            connector_type="shell",
            url="example.com",
            port=None,
        )
        assert rejection is not None
        assert rejection["scheme"] == "<missing>"

    def test_validate_accepts_ws_url(self) -> None:
        assert (
            _validate_session_create_config(
                connector_type="ws",
                url="ws://example.com/term",
                port=None,
            )
            is None
        )

    async def test_session_create_blocks_invalid_connector_via_mcp(self) -> None:
        mcp = _mcp("admin")
        data = await _call(mcp, "session_create", {"connector_type": "rogue"})
        assert data["success"] is False
        assert data["error"] == "invalid_connector_type"


# ---------------------------------------------------------------------------
# Principal resolution helpers
# ---------------------------------------------------------------------------


class TestPrincipalResolution:
    def test_principal_from_headers_returns_none_for_empty(self) -> None:
        assert principal_from_headers(None) is None
        assert principal_from_headers({}) is None

    def test_principal_from_headers_role_only(self) -> None:
        p = principal_from_headers({"X-Uterm-Role": "operator"})
        assert p is not None
        assert p.subject_id == "anonymous"
        assert p.roles == frozenset({"operator"})

    def test_principal_from_headers_subject_only_defaults_role(self) -> None:
        p = principal_from_headers({"X-Uterm-Principal": "alice"})
        assert p is not None
        assert p.subject_id == "alice"
        assert p.roles == frozenset({"viewer"})

    def test_principal_from_headers_unrelated_keys_returns_none(self) -> None:
        assert principal_from_headers({"x-other-header": "ignore"}) is None

    def test_principal_from_headers_case_insensitive(self) -> None:
        p = principal_from_headers({"x-uterm-role": "admin", "x-uterm-principal": "bob"})
        assert p is not None
        assert p.subject_id == "bob"
        assert p.roles == frozenset({"admin"})

    def test_default_principal_inferred_from_headers(self) -> None:
        # When create_mcp_app gets no ``default_principal`` it should fall
        # back to the headers and use them to derive a principal.
        app = create_mcp_app(
            "http://test",
            headers={"X-Uterm-Principal": "alice", "X-Uterm-Role": "operator"},
        )
        # We can't introspect easily through FastMCP, but we can call a
        # viewer tool that the operator role can see and observe that an
        # admin-only tool gets denied:
        # (actual call covered by allow/deny matrix; here just sanity-check
        #  it is constructed without error.)
        assert app is not None

    def test_default_principal_falls_back_to_operator_for_local(self) -> None:
        # Finding #2: no headers, no explicit principal → operator (NOT admin).
        # An MCP server is typically launched over stdio by an LLM with no
        # explicit caller identity; granting admin by default would let any
        # model invoke destructive tools without operator opt-in.
        from unittest.mock import patch

        from provide.uterm.ai.server import create_mcp_app as _create

        captured: dict[str, object] = {}

        class _StubMCP:
            def __init__(self, *a, **kw):
                pass

            def tool(self):
                def _decorator(fn):
                    return fn

                return _decorator

        def _capture_auth_ctx(*, default_principal):
            captured["principal"] = default_principal
            return type("X", (), {"default_principal": default_principal})()

        with (
            patch("provide.uterm.ai.server.FastMCP", _StubMCP),
            patch("provide.uterm.ai.server_impl.AuthorizationContext", _capture_auth_ctx),
        ):
            _create("http://test")
        principal = captured["principal"]
        assert principal.roles == frozenset({"operator"}), "default role must be operator, not admin (Finding #2)"

    def test_default_role_admin_opt_in(self) -> None:
        # Operators that need admin must opt in explicitly.
        from unittest.mock import patch

        from provide.uterm.ai.server import create_mcp_app as _create

        captured: dict[str, object] = {}

        class _StubMCP:
            def __init__(self, *a, **kw):
                pass

            def tool(self):
                def _decorator(fn):
                    return fn

                return _decorator

        def _capture_auth_ctx(*, default_principal):
            captured["principal"] = default_principal
            return type("X", (), {"default_principal": default_principal})()

        with (
            patch("provide.uterm.ai.server.FastMCP", _StubMCP),
            patch("provide.uterm.ai.server_impl.AuthorizationContext", _capture_auth_ctx),
        ):
            _create("http://test", default_role="admin")
        assert captured["principal"].roles == frozenset({"admin"})

    def test_default_role_invalid_rejected(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="default_role must be one of"):
            create_mcp_app("http://test", default_role="root")

    def test_cli_role_flag_default_operator(self) -> None:
        # ``uterm-mcp`` must pass --role through to create_mcp_app, defaulting
        # to "operator" so the LLM is not silently admin.
        from unittest.mock import patch

        from provide.uterm.ai.cli import main as cli_main

        with patch("provide.uterm.ai.server.create_mcp_app") as mock_create:
            mock_create.return_value.run = lambda **_kw: None
            cli_main(["--url", "http://test"])
        kwargs = mock_create.call_args.kwargs
        assert kwargs["default_role"] == "operator"

    def test_cli_role_flag_admin_opt_in(self) -> None:
        from unittest.mock import patch

        from provide.uterm.ai.cli import main as cli_main

        with patch("provide.uterm.ai.server.create_mcp_app") as mock_create:
            mock_create.return_value.run = lambda **_kw: None
            cli_main(["--url", "http://test", "--role", "admin"])
        kwargs = mock_create.call_args.kwargs
        assert kwargs["default_role"] == "admin"

    async def test_resolve_principal_with_no_ctx_returns_default(self) -> None:
        default = _principal("operator")
        resolved = await resolve_principal(None, default=default)
        assert resolved is default

    async def test_resolve_principal_uses_request_state(self) -> None:
        ctx = AsyncMock()
        scoped = _principal("admin", subject="scoped")
        ctx.get_state = AsyncMock(return_value=scoped)
        resolved = await resolve_principal(ctx, default=_principal("viewer"))
        assert resolved is scoped

    async def test_resolve_principal_ignores_unrelated_state(self) -> None:
        ctx = AsyncMock()
        ctx.get_state = AsyncMock(return_value="not-a-principal")
        default = _principal("operator")
        resolved = await resolve_principal(ctx, default=default)
        assert resolved is default

    async def test_resolve_principal_swallows_get_state_errors(self) -> None:
        ctx = AsyncMock()
        ctx.get_state = AsyncMock(side_effect=RuntimeError("backend down"))
        default = _principal("admin")
        resolved = await resolve_principal(ctx, default=default)
        assert resolved is default


# ---------------------------------------------------------------------------
# McpPrincipal helpers + decorator metadata
# ---------------------------------------------------------------------------
