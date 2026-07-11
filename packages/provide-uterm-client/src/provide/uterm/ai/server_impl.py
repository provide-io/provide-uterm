#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FastMCP server exposing the full provide-uterm control plane.

Factory function ``create_mcp_app()`` returns a ready-to-run :class:`FastMCP`
instance with 21 tools covering session management, hijack lifecycle, and
worker control.

The tool handlers themselves live in two cohesive sibling modules so no single
file grows unbounded:

* :mod:`provide.uterm.ai.server_tools_hijack` — hijack lifecycle + server/worker
  control (10 tools), and
* :mod:`provide.uterm.ai.server_tools_session` — session management, real-time
  event subscription, fan-out, and annotation (11 tools).

The input-hardening validators and snapshot shaping live in
:mod:`provide.uterm.ai.server_validators`; their public names
(``_clean_snapshot``, ``_trim_tail``, ``_validate_session_create_config``,
``_compile_user_pattern``) are re-exported here so existing callers/tests that
import them from ``provide.uterm.ai.server_impl`` keep working unchanged.

Every tool handler is wrapped by the authorization chokepoint
(:mod:`provide.uterm.ai.auth`); roles are declared once in
:mod:`provide.uterm.ai.policy` and an unguarded tool will be refused by
the dispatcher rather than silently exposed.

Usage::

    from provide.uterm.ai import create_mcp_app

    app = create_mcp_app("http://localhost:8780")
    app.run(transport="stdio")
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from fastmcp import FastMCP

from provide.uterm.ai.auth import (
    AuthorizationContext,
    McpPrincipal,
    principal_from_headers,
)
from provide.uterm.ai.server_tools_gui import register_gui_tools
from provide.uterm.ai.server_tools_hijack import register_hijack_tools
from provide.uterm.ai.server_tools_session import register_session_tools

# Re-export the input-hardening validators so callers/tests importing them from
# ``provide.uterm.ai.server_impl`` (the historical home) keep working after the
# split into ``server_validators``.
from provide.uterm.ai.server_validators import (
    _clean_snapshot,
    _compile_user_pattern,
    _is_internal_host,
    _reject_bad_id,
    _reject_bad_pattern,
    _trim_tail,
    _validate_session_create_config,
)
from provide.uterm.client.hijack import HijackClient
from provide.uterm.client.sanitizer import unescape_keys

__all__ = [
    "TOOL_COUNT",
    "AuthorizationContext",
    "FastMCP",
    "HijackClient",
    "McpPrincipal",
    "_clean_snapshot",
    "_compile_user_pattern",
    "_is_internal_host",
    "_reject_bad_id",
    "_reject_bad_pattern",
    "_trim_tail",
    "_unescape_keys",
    "_validate_session_create_config",
    "create_mcp_app",
    "principal_from_headers",
]

TOOL_COUNT = 26

# Backwards-compatible alias: the canonical unescape logic now lives in
# ``provide.uterm.client.sanitizer`` so both MCP code paths share it, but the
# original private name remains importable for existing callers/tests.
_unescape_keys = unescape_keys


def create_mcp_app(
    base_url: str,
    *,
    default_principal: McpPrincipal | None = None,
    default_role: str = "operator",
    **client_kwargs: Any,
) -> FastMCP:
    """Create a FastMCP app with all provide-uterm tools.

    Parameters
    ----------
    base_url:
        Root URL of the provide-uterm server.
    default_principal:
        Principal applied when no per-request authentication is available.
        When ``None``, the principal is inferred from the ``X-Uterm-Principal``
        / ``X-Uterm-Role`` headers in ``client_kwargs["headers"]`` (so legacy
        callers that supplied auth headers continue to work), falling back to
        a principal carrying ``default_role`` for stdio/local development.
    default_role:
        Role assigned to the fallback ``McpPrincipal`` when no headers/principal
        are supplied.  Defaults to ``"operator"``; operators that need admin
        must opt in explicitly (``--role admin`` on the CLI).  Must be one of
        ``admin``, ``operator``, ``viewer``.
    **client_kwargs:
        Forwarded to :class:`HijackClient` (``entity_prefix``,
        ``headers``, ``timeout``, ``transport``).
    """
    if default_role not in {"admin", "operator", "viewer"}:
        raise ValueError(f"default_role must be one of 'admin', 'operator', 'viewer'; got {default_role!r}")
    client = HijackClient(base_url, **client_kwargs)

    if default_principal is None:
        default_principal = principal_from_headers(client_kwargs.get("headers")) or McpPrincipal(
            subject_id="local",
            # An MCP server is typically launched over stdio by an LLM with no
            # explicit caller identity; defaulting to admin would let any model
            # invoke destructive tools (e.g. session delete, hijack release)
            # without the operator opting in.  Operators that need admin must
            # pass ``--role admin`` on the CLI or supply ``X-Uterm-Role: admin``.
            roles=frozenset({default_role}),
        )
    auth_ctx = AuthorizationContext(default_principal=default_principal)

    @contextlib.asynccontextmanager
    async def _lifespan(_app: FastMCP) -> AsyncIterator[None]:
        yield
        await client.__aexit__(None, None, None)

    mcp = FastMCP("uterm", lifespan=_lifespan)

    register_hijack_tools(mcp, client, auth_ctx)
    register_session_tools(mcp, client, auth_ctx)
    register_gui_tools(mcp, client, auth_ctx)

    return mcp
