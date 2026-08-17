#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the MCP authorization chokepoint: principal, decorator, denial shape.

Split from a single ``test_mcp_authz.py``; ``test_mcp_authz_part1.py`` covers
the policy table, the full allow/deny matrix, ``session_create`` connector
validation, and header-based principal resolution. This file covers what's
left:

1. :class:`~provide.uterm.ai.auth.McpPrincipal` itself — ``primary_role``,
   ``has_at_least``, and the anonymous/viewer default.
2. The :func:`~provide.uterm.ai.auth.authorized` decorator's own behavior:
   metadata stamping, invoking vs. short-circuiting the wrapped body, the
   transport-authenticated identity overriding the default's subject id, the
   fallback to the configured default on an unauthenticated transport, and
   survival of an unbound ``ctx.request_context``.
3. The shape of a denial: :func:`~provide.uterm.ai.auth.deny_payload`'s
   envelope and :class:`~provide.uterm.ai.auth.AuthorizationDenied`'s message.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from provide.uterm.ai.auth import (
    AuthorizationContext,
    AuthorizationDenied,
    McpPrincipal,
    authorized,
    deny_payload,
    resolve_principal,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _principal(role: str, subject: str = "tester") -> McpPrincipal:
    """Build a single-role principal for tests."""
    return McpPrincipal(subject_id=subject, roles=frozenset({role}))


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

    async def test_decorator_uses_authenticated_subject(self) -> None:
        """A transport-authenticated subject overrides the default's identity.

        Roles still come from the configured default: authenticated_principal()
        returns an identity string, not a role set.
        """
        auth_ctx = AuthorizationContext(default_principal=_principal("admin", subject="default"))
        body = AsyncMock(return_value={"success": True, "via": "body"})
        req_ctx = MagicMock()

        decorated = authorized("session_create", auth_ctx)(body)
        with patch(
            "provide.uterm.ai.auth.authenticated_principal",
            return_value="alice@example.com",
        ) as mock_authenticated_principal:
            result = await decorated(ctx=req_ctx)

        assert result == {"success": True, "via": "body"}
        body.assert_awaited_once()
        # Pins the actual wiring: resolve_principal must pass the request
        # context (ctx.request_context), not the bare ctx, into
        # authenticated_principal() -- getting this wrong would silently
        # break identity binding without any test noticing.
        mock_authenticated_principal.assert_called_once_with(req_ctx.request_context)

    async def test_authenticated_subject_denial_names_authenticated_identity(self) -> None:
        """A denial for an authenticated-but-underprivileged subject names *that*
        subject, not the configured default's, proving the substitution reaches
        an operator-visible field (``deny_payload``'s ``principal`` key) and
        isn't merely internal bookkeeping.
        """
        auth_ctx = AuthorizationContext(default_principal=_principal("viewer", subject="default"))
        body = AsyncMock(return_value={"success": True, "via": "body"})
        req_ctx = MagicMock()

        decorated = authorized("session_create", auth_ctx)(body)
        with patch(
            "provide.uterm.ai.auth.authenticated_principal",
            return_value="alice@example.com",
        ):
            result = await decorated(ctx=req_ctx)

        body.assert_not_awaited()
        assert result["error"] == "authorization_denied"
        assert result["required_role"] == "admin"
        assert result["principal"] == "alice@example.com"

    async def test_decorator_falls_back_to_default_on_unauthenticated_transport(self) -> None:
        """stdio has no principal binding; the configured default applies."""
        auth_ctx = AuthorizationContext(default_principal=_principal("viewer", subject="default"))
        body = AsyncMock(return_value={"success": True, "via": "body"})
        req_ctx = MagicMock()

        decorated = authorized("session_create", auth_ctx)(body)
        with patch("provide.uterm.ai.auth.authenticated_principal", return_value=None):
            result = await decorated(ctx=req_ctx)

        body.assert_not_awaited()
        assert result["error"] == "authorization_denied"
        assert result["required_role"] == "admin"
        assert result["principal"] == "default"

    async def test_resolve_principal_survives_unbound_request_context(self) -> None:
        """Context.request_context raises when unbound — it must not escape."""
        default = _principal("operator", subject="default")
        ctx = MagicMock()
        type(ctx).request_context = PropertyMock(side_effect=ValueError("no active request"))

        resolved = await resolve_principal(ctx, default=default)

        assert resolved is default


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
