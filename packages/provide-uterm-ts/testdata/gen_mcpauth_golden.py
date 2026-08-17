#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the MCP authorization chokepoint.

The table saying which role each tool needs is one thing; this is what happens
when a call arrives. Every tool goes through it, and three details decide what
a model can actually do:

* **A principal holding several roles is judged on the best of them.** The
  check asks whether *any* role held meets the minimum, so an operator who is
  also an admin gets an admin's reach. A principal holding none meets nothing.
* **Where the principal comes from.** The transport's own authenticated
  identity first — the (client, issuer, subject) triple the MCP SDK binds
  when the transport actually authenticates the caller — and the server's
  configured default only when there is none. There is no per-request state
  bag and no header override at authorization time: headers can only shape
  the *default* principal, at server construction, never a per-call
  resolution. A context that fails to answer is treated as having said
  nothing rather than as an error, so a broken lookup cannot become a
  privilege.
* **A refusal is a result, not an exception.** It has the same shape as every
  other tool answer, so a caller branches on it rather than special-casing —
  and it names the tool, the role required and the roles held, which is what
  an operator needs to fix the grant.

The default principal is ``anonymous`` holding ``viewer``, which is the least
this can be.

# uv-package: provide-uterm-client

Usage (from the repository root)::

    uv run --package provide-uterm-client python \\
        packages/provide-uterm-ts/testdata/gen_mcpauth_golden.py
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from provide.uterm.ai import auth as mcp_auth

if TYPE_CHECKING:
    from collections.abc import Iterator

OUT = Path(__file__).resolve().parent / "mcpauth_golden.json"

PRINCIPALS: list[tuple[str, str, list[str]]] = [
    ("an admin", "ada", ["admin"]),
    ("an operator", "ada", ["operator"]),
    ("a viewer", "ada", ["viewer"]),
    ("somebody holding two roles", "ada", ["viewer", "admin"]),
    ("somebody holding operator and viewer", "ada", ["viewer", "operator"]),
    ("somebody holding no role at all", "ada", []),
    ("somebody holding a role nobody defined", "ada", ["superuser"]),
    ("somebody holding a role in capitals", "ada", ["Admin"]),
    ("the default principal", "anonymous", ["viewer"]),
]

TOOLS = ["session_list", "session_read", "session_connect", "hijack_begin", "session_create", "gui_key"]


class ContextStub:
    """A request context whose ``request_context`` access succeeds.

    ``authenticated_principal()`` (the SDK function ``resolve_principal`` now
    delegates to) never reads its argument — it reads the authenticated
    identity out of a contextvar the SDK's own auth middleware populates per
    request. So this stub's only job is to make ``ctx.request_context``
    return *something* without raising; what it returns is irrelevant to the
    resolved principal. Whether that identity shows up is controlled instead
    by :func:`_authenticated_as`, below.
    """

    request_context = "request-context-sentinel"


class BrokenContext:
    """A request context whose ``request_context`` access raises."""

    @property
    def request_context(self) -> Any:
        raise RuntimeError("state unavailable")


def _principal(subject: str, roles: list[str]) -> mcp_auth.McpPrincipal:
    return mcp_auth.McpPrincipal(subject_id=subject, roles=frozenset(roles))


@contextmanager
def _authenticated_as(client_id: str, subject: str | None) -> Iterator[None]:
    """Bind the SDK's auth contextvar so ``authenticated_principal()`` sees an identity.

    In a real deployment, ``AuthContextMiddleware`` does this once per HTTP
    request after the transport verifies a bearer token. The generator has no
    transport to drive, so it binds the same contextvar directly — this is
    the only way to make ``authenticated_principal()`` return non-``None``,
    since it ignores the ``ctx`` object entirely.
    """
    token = AccessToken(token="test-token", client_id=client_id, scopes=[], subject=subject)  # noqa: S106
    reset_token = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(reset_token)


async def _resolution() -> list[dict[str, Any]]:
    """Where the principal comes from, in the order it is looked for.

    Only two things can make ``resolve_principal`` diverge from the default:
    an unbound ``ctx`` (``None`` or one whose ``request_context`` raises),
    and whether the transport actually authenticated a caller. There is no
    longer a per-request state bag to fake independently of authentication,
    so this corpus is honest about covering exactly those two axes rather
    than a wider "arbitrary stored value" surface that no longer exists.
    """
    default = _principal("configured", ["operator"])
    cases: list[dict[str, Any]] = []
    for name, ctx, authenticated_as in (
        ("no context at all", None, None),
        ("an unauthenticated transport", ContextStub(), None),
        ("an authenticated transport", ContextStub(), ("web-client", "alice")),
        ("a context whose request_context access fails", BrokenContext(), None),
    ):
        if authenticated_as is None:
            resolved = await mcp_auth.resolve_principal(ctx, default=default)
        else:
            client_id, subject = authenticated_as
            with _authenticated_as(client_id, subject):
                resolved = await mcp_auth.resolve_principal(ctx, default=default)
        cases.append(
            {
                "name": name,
                "subject_id": resolved.subject_id,
                "roles": sorted(resolved.roles),
            }
        )
    return cases


async def main_async() -> None:
    default = mcp_auth.McpPrincipal()
    corpus = {
        "default_principal": {"subject_id": default.subject_id, "roles": sorted(default.roles)},
        "principals": [
            {
                "name": name,
                "subject_id": subject,
                "roles": sorted(roles),
                "primary_role": _principal(subject, roles).primary_role,
                "meets": {
                    minimum: _principal(subject, roles).has_at_least(minimum)  # type: ignore[arg-type]
                    for minimum in ("viewer", "operator", "admin")
                },
                "may": {tool: _principal(subject, roles).has_at_least(mcp_auth.required_role(tool)) for tool in TOOLS},
            }
            for name, subject, roles in PRINCIPALS
        ],
        "resolution": await _resolution(),
        "denials": [
            mcp_auth.deny_payload(
                mcp_auth.AuthorizationDenied(
                    tool=tool,
                    principal=_principal(subject, roles),
                    required=mcp_auth.required_role(tool),
                )
            )
            for tool, subject, roles in (
                ("hijack_begin", "ada", ["viewer"]),
                ("session_create", "ada", ["operator", "viewer"]),
                ("session_read", "anonymous", []),
            )
        ],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['principals'])} principals)")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
