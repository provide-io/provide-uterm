#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Authorization chokepoint for MCP tool dispatch.

All MCP tool handlers go through :func:`authorize` (applied via
:func:`authorized` decorator) before their bodies execute.  The chokepoint:

1. Resolves the calling :class:`McpPrincipal` via the transport's
   authenticated identity (when available, from :func:`mcp.server.mcpserver.authenticated_principal`),
   or falls back to the server's configured default principal.
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
from typing import Any, Protocol, TypeVar, cast

from mcp.server.mcpserver import authenticated_principal

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
    """Subset of the MCP ``Context`` the chokepoint uses (kept narrow for typing)."""

    @property
    def request_context(self) -> Any: ...


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

# MCP 2.0 removed fastmcp's per-request state bag (``ctx.get_state``), so the
# principal is no longer looked up by key. The SDK's own binding —
# ``authenticated_principal`` — supplies the authenticated (client, issuer,
# subject) identity directly, and returns None on unauthenticated transports
# such as stdio.


def principal_from_headers(headers: dict[str, str] | None) -> McpPrincipal | None:
    """Build a principal from ``X-Uterm-Principal`` / ``X-Uterm-Role`` headers.

    Returns ``None`` when neither header is present.  Header lookup is
    case-insensitive.

    Security boundary: these headers are trusted only because they are
    supplied locally by the operator launching the stdio server, via
    ``client_kwargs["headers"]``. They are NOT a remote caller's assertion.
    If an HTTP transport is ever enabled for this server, this path must be
    removed or gated behind a verified token first — MCP 2.0's
    ``Context.headers`` is explicit that client-supplied headers are never an
    identity assertion.
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


def _authenticated_subject(ctx: _ContextLike) -> str | None:
    """Return the transport-authenticated subject id, or None.

    ``Context.request_context`` raises when no request is bound rather than
    returning None, so the access is guarded: an unbound context is a
    "no authenticated identity" answer, not an error worth propagating out of
    an authorization check.
    """
    try:
        request_context = ctx.request_context
    except Exception:
        return None
    return authenticated_principal(request_context)


async def resolve_principal(
    ctx: _ContextLike | None,
    *,
    default: McpPrincipal,
) -> McpPrincipal:
    """Resolve the principal for the current MCP request.

    Lookup order:

    1. The transport-authenticated subject, when the transport binds one
       (:func:`mcp.server.mcpserver.authenticated_principal`). Roles come from
       the configured *default*, because that binding carries identity, not
       authorisation.
    2. Configured server *default* (passed in by ``create_mcp_app``).

    Kept ``async`` despite no longer awaiting anything, so that
    :func:`authorized` and its tests need no change.
    """
    if ctx is not None:
        subject = _authenticated_subject(ctx)
        if subject is not None:
            return McpPrincipal(subject_id=subject, roles=default.roles)
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

    Principal resolution uses the transport-authenticated identity when
    available (via :func:`mcp.server.mcpserver.authenticated_principal`), and
    falls back to ``auth_ctx.default_principal``. The decorator preserves the
    wrapped function's signature via :func:`functools.wraps` so that the MCP
    server can introspect parameter types as if no decoration were applied.
    """
    minimum = required_role(tool_name)

    def _decorator(fn: F) -> F:
        @wraps(fn)
        async def _wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx = kwargs.get("ctx")
            principal = await resolve_principal(
                cast("_ContextLike | None", ctx),
                default=auth_ctx.default_principal,
            )
            if not principal.has_at_least(minimum):
                err = AuthorizationDenied(tool=tool_name, principal=principal, required=minimum)
                return deny_payload(err)
            return await fn(*args, **kwargs)

        # Stash metadata for tests / introspection. Using ``setattr`` makes
        # the dynamic attribute attachment explicit to both mypy and ty
        # (both type checkers treat direct ``obj.attr = …`` on a typed
        # callable as an unresolved-attribute error; ``setattr`` is the
        # canonical opt-out).
        setattr(_wrapper, "__uterm_tool_name__", tool_name)  # noqa: B010
        setattr(_wrapper, "__uterm_required_role__", minimum)  # noqa: B010
        # ``functools.wraps`` returns ``_Wrapped[…]`` rather than the
        # original ``F``; the cast tells both type checkers the wrapper
        # honours the same callable shape as the wrapped function.
        return cast("F", _wrapper)

    return _decorator
