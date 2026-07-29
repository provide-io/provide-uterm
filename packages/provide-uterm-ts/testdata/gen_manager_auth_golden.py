#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the swarm manager's auth boundary.

The manager spawns and kills processes across a fleet, so its token check is
the boundary between an operator and a worker that has been taken over.

* **Two privilege levels.** The operator token authorizes every route. The
  worker tokens authorize only the two self-report routes — status and
  register — and are refused on spawn, kill, prune and every read.
* **A worker's token is bound to its own agent id.** The fleet secret is an
  HMAC key that never leaves the manager; each worker gets
  `HMAC(secret, its own id)`. The derivation is one-way, so a worker holding
  its own token cannot compute another's, and the token is checked against
  the id *in the path* — which is what stops one compromised worker
  reporting as, or registering over, any other.
* **The route patterns are fully anchored.** `/agent/x/statusfoo`, a nested
  id, a trailing slash and a query string all fail to match, so none of them
  reaches the low-privilege branch.
* **Refusal has a shape.** An HTTP caller gets a 401; a WebSocket caller is
  accepted and then closed with 4403, because a WebSocket cannot be handed a
  status code any other way.

Everything here is driven: the middleware is run over real ASGI scopes with a
recording inner app, so what is recorded is what happened, not what the
docstrings say happens.

# uv-package: provide-uterm-platform

Usage (from the repository root)::

    uv run --package provide-uterm-platform python \\
        packages/provide-uterm-ts/testdata/gen_manager_auth_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from provide.uterm.manager import auth as manager_auth

OUT = Path(__file__).resolve().parent / "manager_auth_golden.json"

SECRET = "fleet-secret"  # noqa: S105
OPERATOR_TOKEN = "operator-token"  # noqa: S105
FLEET_TOKEN = "fleet-token"  # noqa: S105

DERIVED: list[tuple[str, str, str]] = [
    ("an ordinary agent", SECRET, "agent-1"),
    ("another agent under the same secret", SECRET, "agent-2"),
    ("the same agent under another secret", "other-secret", "agent-1"),
    ("an agent with no id", SECRET, ""),
    ("an id outside ASCII", SECRET, "agent-☃"),
    ("an id that looks like a path", SECRET, "a/b"),
    ("a long id", SECRET, "x" * 256),
    ("an id differing in one character", SECRET, "agent-3"),
    ("an empty secret", "", "agent-1"),
]

# (method, path) -> the agent id the anchored patterns capture, or nothing.
ROUTES: list[tuple[str, str, str]] = [
    ("a status report", "POST", "/agent/agent-1/status"),
    ("a registration", "POST", "/agent/agent-1/register"),
    ("a status report read", "GET", "/agent/agent-1/status"),
    ("a status report by another verb", "PUT", "/agent/agent-1/status"),
    ("a path with something after status", "POST", "/agent/agent-1/statusfoo"),
    ("a path with something before status", "POST", "/agent/agent-1/xstatus"),
    ("a nested agent id", "POST", "/agent/a/b/status"),
    ("a trailing slash", "POST", "/agent/agent-1/status/"),
    ("a leading segment", "POST", "/api/agent/agent-1/status"),
    ("an empty agent id", "POST", "/agent//status"),
    ("a query string left on the path", "POST", "/agent/agent-1/status?x=1"),
    ("an agent id that is a dot", "POST", "/agent/./status"),
    ("an agent id that is two dots", "POST", "/agent/../status"),
    ("an id with an escaped slash", "POST", "/agent/a%2Fb/status"),
    ("the spawn route", "POST", "/spawn"),
    ("a kill route", "POST", "/agent/agent-1/kill"),
    ("the root", "GET", "/"),
    ("nothing at all", "POST", ""),
]


def _authorized_cases() -> list[dict[str, Any]]:
    """Every token against every route, under both enforcement settings."""
    cases: list[dict[str, Any]] = []
    tokens: list[tuple[str, str]] = [
        ("the operator token", OPERATOR_TOKEN),
        ("the fleet token", FLEET_TOKEN),
        ("this agent's own derived token", manager_auth.derive_agent_token(SECRET, "agent-1")),
        ("another agent's derived token", manager_auth.derive_agent_token(SECRET, "agent-2")),
        ("a token derived under the wrong secret", manager_auth.derive_agent_token("other-secret", "agent-1")),
        ("no token at all", ""),
        ("a token that is nearly the operator's", OPERATOR_TOKEN + " "),
        ("the operator token in capitals", OPERATOR_TOKEN.upper()),
    ]
    targets: list[tuple[str, str, str]] = [
        ("its own status route", "POST", "/agent/agent-1/status"),
        ("another agent's status route", "POST", "/agent/agent-2/status"),
        ("its own register route", "POST", "/agent/agent-1/register"),
        ("the spawn route", "POST", "/spawn"),
        ("a read", "GET", "/agents"),
    ]
    for enforce in (False, True):
        for has_secret in (False, True):
            for token_name, token in tokens:
                middleware = manager_auth.TokenAuthMiddleware(
                    app=None,
                    token=OPERATOR_TOKEN,
                    worker_token=FLEET_TOKEN,
                    worker_secret=SECRET if has_secret else None,
                    enforce_per_agent_worker_token=enforce,
                )
                for target_name, method, path in targets:
                    cases.append(
                        {
                            "enforce_per_agent": enforce,
                            "has_secret": has_secret,
                            "token": token_name,
                            "target": target_name,
                            "method": method,
                            "path": path,
                            "authorized": middleware._is_authorized(token, path, method),
                        }
                    )
    return cases


def _no_fleet_token_cases() -> list[dict[str, Any]]:
    """With no fleet token configured, the self-report routes stay operator-only."""
    middleware = manager_auth.TokenAuthMiddleware(
        app=None,
        token=OPERATOR_TOKEN,
        worker_token=None,
        worker_secret=None,
    )
    return [
        {
            "token": name,
            "authorized": middleware._is_authorized(token, "/agent/agent-1/status", "POST"),
        }
        for name, token in (
            ("the operator token", OPERATOR_TOKEN),
            ("the fleet token", FLEET_TOKEN),
            ("a derived token", manager_auth.derive_agent_token(SECRET, "agent-1")),
            ("nothing", ""),
        )
    ]


SCOPES: list[tuple[str, dict[str, Any]]] = [
    (
        "a bearer header",
        {"type": "http", "method": "POST", "headers": [(b"authorization", b"Bearer abc")]},
    ),
    (
        "a bearer header with spaces around the token",
        {"type": "http", "method": "POST", "headers": [(b"authorization", b"Bearer   abc  ")]},
    ),
    (
        "a bearer header in lower case",
        {"type": "http", "method": "POST", "headers": [(b"authorization", b"bearer abc")]},
    ),
    (
        "a bearer header with no token",
        {"type": "http", "method": "POST", "headers": [(b"authorization", b"Bearer ")]},
    ),
    (
        "an authorization header of another kind",
        {"type": "http", "method": "POST", "headers": [(b"authorization", b"Basic abc")]},
    ),
    (
        "an api token header",
        {"type": "http", "method": "POST", "headers": [(b"x-api-token", b"abc")]},
    ),
    (
        "an api token header with spaces",
        {"type": "http", "method": "POST", "headers": [(b"x-api-token", b"  abc  ")]},
    ),
    (
        "both headers",
        {
            "type": "http",
            "method": "POST",
            "headers": [(b"authorization", b"Bearer from-bearer"), (b"x-api-token", b"from-api")],
        },
    ),
    ("no headers at all", {"type": "http", "method": "POST", "headers": []}),
    (
        "a preflight",
        {"type": "http", "method": "OPTIONS", "headers": [(b"authorization", b"Bearer abc")]},
    ),
    ("a websocket with a token", {"type": "websocket", "query_string": b"token=abc"}),
    ("a websocket with spaces around it", {"type": "websocket", "query_string": b"token=%20abc%20"}),
    ("a websocket with no token", {"type": "websocket", "query_string": b""}),
    ("a websocket with an empty token", {"type": "websocket", "query_string": b"token="}),
    ("a websocket with two tokens", {"type": "websocket", "query_string": b"token=one&token=two"}),
    ("a websocket with another parameter", {"type": "websocket", "query_string": b"other=abc"}),
    (
        "a websocket carrying a header instead",
        {"type": "websocket", "query_string": b"", "headers": [(b"authorization", b"Bearer abc")]},
    ),
]


async def _drive(case: dict[str, Any]) -> dict[str, Any]:
    """Run one request through the real middleware, recording what came back."""
    reached: list[str] = []
    sent: list[dict[str, Any]] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        reached.append("inner")

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        recorded = {key: value for key, value in message.items() if key != "headers"}
        if isinstance(recorded.get("body"), bytes):
            recorded["body"] = recorded["body"].decode("utf-8")
        sent.append(recorded)

    middleware = manager_auth.TokenAuthMiddleware(
        app=inner,
        token=OPERATOR_TOKEN,
        worker_token=FLEET_TOKEN,
        worker_secret=SECRET,
        public_paths=frozenset({"/health"}),
        public_prefixes=("/static/",),
    )
    await middleware(case["scope"], receive, send)
    return {
        "name": case["name"],
        "scope": {
            "type": case["scope"].get("type"),
            "method": case["scope"].get("method"),
            "path": case["scope"].get("path"),
            "query_string": case["scope"].get("query_string", b"").decode("utf-8"),
            "headers": [[key.decode(), value.decode()] for key, value in case["scope"].get("headers", [])],
        },
        "reached_inner": bool(reached),
        "sent": sent,
    }


REQUESTS: list[dict[str, Any]] = [
    {
        "name": "an operator spawning",
        "scope": {
            "type": "http",
            "method": "POST",
            "path": "/spawn",
            "headers": [(b"authorization", f"Bearer {OPERATOR_TOKEN}".encode())],
        },
    },
    {
        "name": "a worker spawning with its own token",
        "scope": {
            "type": "http",
            "method": "POST",
            "path": "/spawn",
            "headers": [(b"authorization", f"Bearer {manager_auth.derive_agent_token(SECRET, 'agent-1')}".encode())],
        },
    },
    {
        "name": "a worker reporting its own status",
        "scope": {
            "type": "http",
            "method": "POST",
            "path": "/agent/agent-1/status",
            "headers": [(b"authorization", f"Bearer {manager_auth.derive_agent_token(SECRET, 'agent-1')}".encode())],
        },
    },
    {
        "name": "a worker reporting as another agent",
        "scope": {
            "type": "http",
            "method": "POST",
            "path": "/agent/agent-2/status",
            "headers": [(b"authorization", f"Bearer {manager_auth.derive_agent_token(SECRET, 'agent-1')}".encode())],
        },
    },
    {
        "name": "nobody at all",
        "scope": {"type": "http", "method": "GET", "path": "/agents", "headers": []},
    },
    {
        "name": "a public path",
        "scope": {"type": "http", "method": "GET", "path": "/health", "headers": []},
    },
    {
        "name": "something under a public prefix",
        "scope": {"type": "http", "method": "GET", "path": "/static/app.js", "headers": []},
    },
    {
        "name": "a path that merely starts like a public one",
        "scope": {"type": "http", "method": "GET", "path": "/healthz", "headers": []},
    },
    {
        "name": "a preflight with no token",
        "scope": {"type": "http", "method": "OPTIONS", "path": "/spawn", "headers": []},
    },
    {
        "name": "a websocket with the operator token",
        "scope": {"type": "websocket", "path": "/ws", "query_string": f"token={OPERATOR_TOKEN}".encode()},
    },
    {
        "name": "a websocket with nothing",
        "scope": {"type": "websocket", "path": "/ws", "query_string": b""},
    },
    {
        "name": "a lifespan message",
        "scope": {"type": "lifespan"},
    },
]


async def main_async() -> None:
    corpus = {
        "secret": SECRET,
        "operator_token": OPERATOR_TOKEN,
        "fleet_token": FLEET_TOKEN,
        "derived": [
            {
                "name": name,
                "secret": secret,
                "agent_id": agent_id,
                "token": manager_auth.derive_agent_token(secret, agent_id),
            }
            for name, secret, agent_id in DERIVED
        ],
        "routes": [
            {
                "name": name,
                "method": method,
                "path": path,
                "agent_id": manager_auth._extract_self_report_agent_id(path, method),
            }
            for name, method, path in ROUTES
        ],
        "authorized": _authorized_cases(),
        "no_fleet_token": _no_fleet_token_cases(),
        "extracted": [
            {
                "name": name,
                "scope": {
                    "type": scope.get("type"),
                    "method": scope.get("method"),
                    "query_string": scope.get("query_string", b"").decode("utf-8"),
                    "headers": [[key.decode(), value.decode()] for key, value in scope.get("headers", [])],
                },
                "token": token,
                "pass_through": pass_through,
            }
            for name, scope in SCOPES
            for token, pass_through in [
                manager_auth.TokenAuthMiddleware(app=None, token=OPERATOR_TOKEN)._extract_request_token(scope)
            ]
        ],
        "requests": [await _drive(case) for case in REQUESTS],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['authorized'])} authorization cases)")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
