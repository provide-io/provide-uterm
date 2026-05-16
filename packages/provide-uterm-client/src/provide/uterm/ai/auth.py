#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Authorization chokepoint for MCP tool dispatch.

All MCP tool handlers go through :func:`authorize` (applied via
:func:`authorized` decorator) before their bodies execute.  The chokepoint:

1. Resolves the calling :class:`McpPrincipal` from the ambient
   :class:`~fastmcp.Context` (request-scoped state, then transport headers,
   then the server's configured default principal).
2. Looks up the tool's required role in
   :mod:`provide.uterm.ai.policy`.
3. Rejects with a typed :class:`AuthorizationDenied` (returned as a
   structured error dict — never raised across the wire) before the tool
   body runs.

Adding a new tool therefore requires an explicit
:data:`~provide.uterm.ai.policy.TOOL_REQUIRED_ROLES` entry; otherwise
the chokepoint refuses the call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Protocol, TypeVar

from provide.uterm.ai.policy import Role, required_role, role_at_least

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


@dataclass(slots=True, frozen=True)
class McpPrincipal:
    """Principal calling an MCP tool.

    Mirrors the fields used by ``provide.uterm.server.auth.Principal``
    that matter at the MCP boundary.  Kept lightweight: the MCP layer does
    not need OIDC claims, just identity + role(s).
    """

    subject_id: str = "anonymous"
    roles: frozenset[str] = field(default_factory=lambda: frozenset({"viewer"}))

    @property
    def primary_role(self) -> str:
        """Return the highest-privilege role the principal holds."""
        from provide.uterm.ai.policy import role_rank

        if not self.roles:
            return "viewer"
        return max(self.roles, key=role_rank)

    def has_at_least(self, minimum: Role) -> bool:
        """Return True if the principal has a role at least *minimum*."""
        return any(role_at_least(r, minimum) for r in self.roles)


class _ContextLike(Protocol):
    """Subset of fastmcp.Context the chokepoint uses (kept narrow for typing)."""

    async def get_state(self, key: str) -> Any: ...


class AuthorizationDenied(Exception):  # noqa: N818 — domain-modeling name; not all exceptions end in Error.
    """Raised when a principal is not permitted to invoke a tool.

    The chokepoint catches this and converts it into a structured error
    dict so MCP clients receive a deterministic shape rather than an MCP
    transport-level exception.
    """

    def __init__(self, *, tool: str, principal: McpPrincipal, required: Role) -> None:
        self.tool = tool
        self.principal = principal
        self.required = required
        super().__init__(
            f"principal {principal.subject_id!r} (roles={sorted(principal.roles)}) "
            f"is not authorized to call tool {tool!r} (requires {required!r})"
        )


# ---------------------------------------------------------------------------
# Principal resolution.
# ---------------------------------------------------------------------------

# Key used to publish the configured default principal into per-request
# context state.  The MCP server registers an on-init hook that calls
# ``ctx.set_state(_PRINCIPAL_STATE_KEY, principal)`` so tool handlers can
# look it up without re-reading transport headers.
_PRINCIPAL_STATE_KEY = "uterm.mcp.principal"


def principal_from_headers(headers: dict[str, str] | None) -> McpPrincipal | None:
    """Build a principal from ``X-Uterm-Principal`` / ``X-Uterm-Role`` headers.

    Returns ``None`` when neither header is present.  Header lookup is
    case-insensitive.
    """
    if not headers:
        return None
    lowered = {k.lower(): v for k, v in headers.items()}
    subject = lowered.get("x-uterm-principal")
    role = lowered.get("x-uterm-role")
    if subject is None and role is None:
        return None
    return McpPrincipal(
        subject_id=subject or "anonymous",
        roles=frozenset({role}) if role else frozenset({"viewer"}),
    )


async def resolve_principal(
    ctx: _ContextLike | None,
    *,
    default: McpPrincipal,
) -> McpPrincipal:
    """Resolve the principal for the current MCP request.

    Lookup order:

    1. Per-request state under :data:`_PRINCIPAL_STATE_KEY` (e.g. set by
       middleware that authenticated the caller).
    2. Configured server *default* (passed in by ``create_mcp_app``).
    """
    if ctx is not None:
        try:
            stored = await ctx.get_state(_PRINCIPAL_STATE_KEY)
        except Exception:
            stored = None
        if isinstance(stored, McpPrincipal):
            return stored
    return default


# ---------------------------------------------------------------------------
# Chokepoint dispatch.
# ---------------------------------------------------------------------------


def deny_payload(err: AuthorizationDenied) -> dict[str, Any]:
    """Render an :class:`AuthorizationDenied` as a tool-result dict.

    Shape matches the rest of the MCP tool surface (``success`` is False
    and an ``error`` discriminator is included) so that callers can branch
    on it without having to special-case authorization failures.
    """
    return {
        "success": False,
        "error": "authorization_denied",
        "tool": err.tool,
        "required_role": err.required,
        "principal": err.principal.subject_id,
        "principal_roles": sorted(err.principal.roles),
    }


@dataclass(slots=True)
class AuthorizationContext:
    """Bundle of state the chokepoint needs at every call.

    A single instance is created by :func:`create_mcp_app` and closed over
    by the per-tool wrappers.  Holding it in one place lets tests inject a
    different default principal without monkey-patching.
    """

    default_principal: McpPrincipal


def authorized(tool_name: str, auth_ctx: AuthorizationContext) -> Callable[[F], F]:
    """Return a decorator that gates *tool_name* on its required role.

    Principal resolution falls back to ``auth_ctx.default_principal`` for
    every call.  The decorator preserves the wrapped function's signature
    via :func:`functools.wraps` so that fastmcp can introspect parameter
    types as if no decoration were applied.
    """
    minimum = required_role(tool_name)

    def _decorator(fn: F) -> F:
        @wraps(fn)
        async def _wrapper(*args: Any, **kwargs: Any) -> Any:
            principal = auth_ctx.default_principal
            if not principal.has_at_least(minimum):
                err = AuthorizationDenied(tool=tool_name, principal=principal, required=minimum)
                return deny_payload(err)
            return await fn(*args, **kwargs)

        # Stash metadata for tests / introspection.
        _wrapper.__uterm_tool_name__ = tool_name  # type: ignore[attr-defined]
        _wrapper.__uterm_required_role__ = minimum  # type: ignore[attr-defined]
        return _wrapper  # type: ignore[return-value]

    return _decorator
