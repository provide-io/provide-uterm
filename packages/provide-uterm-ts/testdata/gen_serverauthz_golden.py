#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the server's RBAC decisions.

Authentication says who a caller is; this is what that buys them. It is a
table, and a table is the easiest kind of thing to get subtly wrong in a port:
a role granted one capability too many is not a crash, it is a caller who can
delete somebody else's session and a test suite that still passes.

So every cell is recorded from the reference's own
``LocalAuthorizationProvider`` — the capability set each role grants, how a
token's scopes narrow it, and who may read a session of each visibility.

Two of the cases are the ones a naive port gets wrong:

* a **session-scoped** admin (what a tunnel share hands out) holds the
  ``admin`` role and must still fail a global admin check, or the grant
  reaches every other session on the server;
* a token carrying **scopes** is cut down to the intersection of its roles and
  its scopes, so a scoped token can never do more than its roles allow.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_serverauthz_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from provide.uterm.server.authorization import ROLE_CAPABILITIES, LocalAuthorizationProvider
from provide.uterm.server.bridge.identity import Principal
from provide.uterm.server.models import SessionDefinition

OUT = Path(__file__).resolve().parent / "serverauthz_golden.json"

#: The principals every session is judged against.
PRINCIPALS: tuple[dict[str, Any], ...] = (
    {"name": "admin", "subject_id": "root", "roles": ["admin"], "scopes": []},
    {"name": "operator", "subject_id": "op", "roles": ["operator"], "scopes": []},
    {"name": "viewer", "subject_id": "watcher", "roles": ["viewer"], "scopes": []},
    {"name": "no_roles", "subject_id": "nobody", "roles": [], "scopes": []},
    {"name": "owner_viewer", "subject_id": "owner", "roles": ["viewer"], "scopes": []},
    {"name": "share", "subject_id": "share:secret:abcdef", "roles": ["viewer"], "scopes": []},
    {"name": "share_elsewhere", "subject_id": "share:other:abcdef", "roles": ["viewer"], "scopes": []},
    {
        "name": "scoped_out",
        "subject_id": "narrow",
        "roles": ["admin"],
        "scopes": ["session.control.delete"],
        "why": "an admin token whose scopes do not include session.read",
    },
    {
        "name": "scoped_in",
        "subject_id": "narrow",
        "roles": ["admin"],
        "scopes": ["session.read"],
        "why": "the same token, scoped to the one capability being asked about",
    },
    {
        "name": "scoped_star",
        "subject_id": "wide",
        "roles": ["viewer"],
        "scopes": ["*"],
        "why": "the star opts out of narrowing rather than granting everything",
    },
    {
        "name": "session_admin",
        "subject_id": "share-operator",
        "roles": ["admin"],
        "scopes": [],
        "admin_session_scope": "secret",
        "why": "a tunnel share's admin grant, confined to one session",
    },
)

#: The sessions the principals are judged against.
SESSIONS: tuple[dict[str, Any], ...] = (
    {"name": "public", "session_id": "secret", "owner": None, "visibility": "public"},
    {"name": "operator_only", "session_id": "secret", "owner": None, "visibility": "operator"},
    {"name": "private", "session_id": "secret", "owner": None, "visibility": "private"},
    {"name": "owned_private", "session_id": "secret", "owner": "owner", "visibility": "private"},
    {"name": "other_private", "session_id": "other", "owner": None, "visibility": "private"},
)


def _principal(spec: dict[str, Any]) -> Principal:
    return Principal(
        subject_id=str(spec["subject_id"]),
        roles=frozenset(spec["roles"]),
        scopes=frozenset(spec["scopes"]),
        admin_session_scope=spec.get("admin_session_scope"),
    )


def _session(spec: dict[str, Any]) -> SessionDefinition:
    return SessionDefinition(
        session_id=str(spec["session_id"]),
        owner=spec["owner"],
        visibility=spec["visibility"],
    )


async def _build() -> dict[str, Any]:
    provider = LocalAuthorizationProvider()
    records: list[dict[str, Any]] = []
    for principal_spec in PRINCIPALS:
        principal = _principal(principal_spec)
        capabilities = sorted(await provider.capabilities_for(principal))
        reads: dict[str, bool] = {}
        owns: dict[str, bool] = {}
        for session_spec in SESSIONS:
            session = _session(session_spec)
            reads[str(session_spec["name"])] = await provider.can_read_session(principal, session)
            owns[str(session_spec["name"])] = await provider.is_owner(principal, session)
        records.append(
            {
                **principal_spec,
                "capabilities": capabilities,
                "is_admin": await provider.is_admin(principal),
                "can_read_session": reads,
                "is_owner": owns,
            }
        )
    return {
        "note": (
            "Recorded from provide.uterm.server.authorization.LocalAuthorizationProvider — the RBAC "
            "provider a deployment gets unless it configures a webhook policy engine."
        ),
        "role_capabilities": {role: sorted(caps) for role, caps in sorted(ROLE_CAPABILITIES.items())},
        "sessions": list(SESSIONS),
        "principals": records,
    }


def main() -> None:
    payload = asyncio.run(_build())
    OUT.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {OUT} ({len(payload['principals'])} principals)")


if __name__ == "__main__":
    main()
