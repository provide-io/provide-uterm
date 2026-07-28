#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-cloudflare
"""Generate the differential golden corpus for the session runtime's auth.

How a Durable Object decides what a caller may do: a share cookie first, then
a JWT, then the session's own ownership.

**A share cookie names the session it is for.** The cookie is
``uterm_tunnel_{worker_id}``, so a token issued for one session cannot
authorise another — a browser holding several is not a browser that may use
any of them anywhere.

**The control token is admin and the share token is viewer.** Two tokens, two
roles, both compared against stored digests rather than stored tokens.

**A share cookie stops working when the tunnel does.** The expiry is stamped
at issuance, and the check is here as well as in the Worker because the
Durable Object is reachable directly.

**An owner is raised to operator, never lowered.** A session's owner holding a
viewer JWT can read it through the visibility check but would be refused every
mutation; raising them is what makes their own session usable. An admin stays
admin — the elevation is a floor, not an assignment.

Usage (from the repository root)::

    uv run --package provide-uterm-cloudflare python \\
        packages/provide-uterm-ts/testdata/gen_sessionauth_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.cloudflare.auth.jwt import Principal, resolve_role

from provide.uterm.tunnel.token_hash import hash_token

OUT = Path(__file__).with_name("sessionauth_golden.json")

WORKER_ID = "w-session"
CONTROL_TOKEN = "control-token-value"  # noqa: S105 - a corpus fixture
SHARE_TOKEN = "share-token-value"  # noqa: S105 - a corpus fixture


# (name, roles, owner, subject) — what a verified principal resolves to.
ROLE_CASES: list[tuple[str, tuple[str, ...], str | None, str]] = [
    ("an admin", ("admin",), None, "u1"),
    ("an operator", ("operator",), None, "u1"),
    ("a viewer", ("viewer",), None, "u1"),
    ("no role at all", (), None, "u1"),
    # The owner elevation: a floor, not an assignment.
    ("the owner, holding viewer", ("viewer",), "u1", "u1"),
    ("the owner, holding operator", ("operator",), "u1", "u1"),
    ("the owner, holding admin", ("admin",), "u1", "u1"),
    ("somebody else, holding viewer", ("viewer",), "u2", "u1"),
    ("an unowned session", ("viewer",), None, "u1"),
    # An owner recorded as the empty string is not an owner.
    ("an owner of nothing", ("viewer",), "", "u1"),
    ("a subject of nothing", ("viewer",), "u1", ""),
]


def _build() -> dict[str, Any]:
    """Everything the decision table says."""
    return {
        "worker_id": WORKER_ID,
        "cookie_name": f"uterm_tunnel_{WORKER_ID}",
        "control_token": CONTROL_TOKEN,
        "share_token": SHARE_TOKEN,
        "control_hash": hash_token(CONTROL_TOKEN),
        "share_hash": hash_token(SHARE_TOKEN),
        # What each token authorises, which is the whole of the share path.
        "share_roles": [
            {"name": "the control token", "token": CONTROL_TOKEN, "role": "admin"},
            {"name": "the share token", "token": SHARE_TOKEN, "role": "viewer"},
            {"name": "neither", "token": "nonsense", "role": None},
            {"name": "nothing", "token": "", "role": None},
        ],
        "roles": [
            {
                "name": name,
                "roles": list(roles),
                "owner": owner,
                "subject": subject,
                # The reference's own resolution, before the elevation.
                "jwt_role": resolve_role(Principal(subject_id=subject, roles=roles)),
                # And after it, as ``browser_role_for_request`` computes it.
                "result": _elevated(resolve_role(Principal(subject_id=subject, roles=roles)), owner, subject),
            }
            for name, roles, owner, subject in ROLE_CASES
        ],
    }


def _elevated(jwt_role: str, owner: str | None, subject: str) -> str:
    """The reference's elevation, lifted from ``browser_role_for_request``."""
    if jwt_role == "admin":
        return "admin"
    if owner is not None and subject == owner:
        return "operator"
    return jwt_role


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(ROLE_CASES)} role cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
