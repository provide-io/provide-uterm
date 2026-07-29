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
* **Where the principal comes from.** Per-request state first — set by
  whatever authenticated the caller — and the server's configured default only
  when there is none. A context that fails to answer is treated as having said
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
from pathlib import Path
from typing import Any

from provide.uterm.ai import auth as mcp_auth

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


class StateContext:
    """A request context that answers with whatever it was given."""

    def __init__(self, stored: Any) -> None:
        self._stored = stored

    async def get_state(self, key: str) -> Any:
        return self._stored


class BrokenContext:
    """A request context whose lookup fails."""

    async def get_state(self, key: str) -> Any:
        raise RuntimeError("state unavailable")


def _principal(subject: str, roles: list[str]) -> mcp_auth.McpPrincipal:
    return mcp_auth.McpPrincipal(subject_id=subject, roles=frozenset(roles))


async def _resolution() -> list[dict[str, Any]]:
    """Where the principal comes from, in the order it is looked for."""
    default = _principal("configured", ["operator"])
    stored = _principal("from-request", ["admin"])
    cases: list[dict[str, Any]] = []
    for name, ctx in (
        ("no context at all", None),
        ("a context carrying a principal", StateContext(stored)),
        ("a context carrying nothing", StateContext(None)),
        ("a context carrying something else", StateContext({"subject_id": "x"})),
        ("a context whose lookup fails", BrokenContext()),
    ):
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
