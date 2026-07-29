#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the Worker's API dispatch.

What a request to ``/api/...`` is answered with before any handler sees it,
and the reasons the four answers are different.

* **405 rather than 404** when the path is a route but the method is not one
  it takes — with an ``Allow`` header naming what it does take, because a
  caller that guessed the verb deserves to be told rather than left to
  conclude the route does not exist.
* **422 rather than 404** when the path has a route's *shape* but a parameter
  that does not match — an id with a slash in it, say. That distinction is
  the difference between "you spelled the id wrong" and "there is no such
  endpoint", and collapsing it would hide a caller's real mistake.
* **404** only when nothing matches at all.

Then, in order: authentication, then the route's role alternatives, then the
capability. A route with no roles named skips the second — every route is
still authenticated, so an empty list means "any authenticated caller" rather
than "anyone".

The capability table is checked at import: every global route must have a
handler, and no *session* route may have one in the Worker, because a session
route belongs to its Durable Object. A new route with no handler fails the
Worker's import rather than 500-ing at request time.

# uv-package: provide-uterm-cloudflare

Usage (from the repository root)::

    uv run --package provide-uterm-cloudflare python \\
        packages/provide-uterm-ts/testdata/gen_apidispatch_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.api_routes import API_ROUTE_REGISTRY, API_ROUTES, RouteScope
from provide.uterm.cloudflare.entry import route_defs

OUT = Path(__file__).resolve().parent / "apidispatch_golden.json"

REQUESTS: list[tuple[str, str, str]] = [
    ("listing sessions", "GET", "/api/sessions"),
    ("creating a session", "POST", "/api/sessions"),
    ("a method the route does not take", "PUT", "/api/sessions"),
    ("another method it does not take", "PATCH", "/api/sessions"),
    ("a session by id", "GET", "/api/sessions/sess-1"),
    ("a session id with capitals in it", "GET", "/api/sessions/Sess-1"),
    ("an API prefix in capitals", "GET", "/API/sessions"),
    ("deleting a session", "DELETE", "/api/sessions/sess-1"),
    ("a session id with a slash in it", "GET", "/api/sessions/a/b"),
    ("a session id that is empty", "GET", "/api/sessions//hijack"),
    ("a session id with an encoded slash", "GET", "/api/sessions/a%2Fb"),
    ("a session id with a dot in it", "GET", "/api/sessions/a.b"),
    ("a session id with a space in it", "GET", "/api/sessions/a b"),
    ("a session id with a bang in it", "GET", "/api/sessions/a!b"),
    ("a session id longer than the registry takes", "GET", "/api/sessions/" + "a" * 200),
    ("a session id climbing out", "GET", "/api/sessions/../x"),
    ("a bad session id on a method the route lacks", "PUT", "/api/sessions/a.b"),
    ("a path that is not an API path", "GET", "/app/session/sess-1"),
    ("the root", "GET", "/"),
    ("an API path nobody routes", "GET", "/api/nowhere"),
    ("an API path nobody routes, with an id", "GET", "/api/nowhere/sess-1"),
    ("a method in lower case", "get", "/api/sessions"),
    ("a HEAD request", "HEAD", "/api/sessions"),
    ("an OPTIONS request", "OPTIONS", "/api/sessions"),
]


def _dispatch(method: str, path: str) -> dict[str, Any]:
    """What the registry answers, before any handler is reached."""
    if not path.startswith("/api/"):
        return {"outcome": "not_api"}
    match = API_ROUTE_REGISTRY.match(method.upper(), path)
    if match is None:
        allowed = API_ROUTE_REGISTRY.allowed_methods(path)
        if allowed:
            return {
                "outcome": "method_not_allowed",
                "status": 405,
                "allow": [entry.value for entry in allowed],
            }
        if any(route_defs._matches_route_shape(path, route) for route in API_ROUTES):
            return {"outcome": "invalid_route_parameter", "status": 422}
        return {"outcome": "not_found", "status": 404}
    return {
        "outcome": "matched",
        "capability": match.route.capability,
        "scope": match.route.scope.value,
        "roles": sorted(match.route.roles),
        "params": dict(match.params),
    }


SHAPES: list[tuple[str, str]] = [
    ("an exact path", "/api/sessions"),
    ("a parameter in place", "/api/sessions/sess-1"),
    ("a parameter that is empty", "/api/sessions/"),
    ("one segment too many", "/api/sessions/sess-1/extra/more"),
    ("one segment too few", "/api"),
    ("a literal that does not match", "/api/session/sess-1"),
    ("a parameter that is not empty but is refused", "/api/sessions/a.b"),
    ("two parameters", "/api/sessions/sess-1/hijack"),
    ("nothing at all", ""),
    ("a bare slash", "/"),
]


def main() -> None:
    corpus = {
        "global_capabilities": sorted(route_defs.GLOBAL_CAPABILITIES),
        "session_capabilities": sorted({route.capability for route in API_ROUTES if route.scope is RouteScope.SESSION}),
        "requests": [
            {"name": name, "method": method, "path": path, **_dispatch(method, path)} for name, method, path in REQUESTS
        ],
        # Shape matching on its own: the 422-versus-404 decision rests on it.
        "shapes": [
            {
                "name": name,
                "path": path,
                "matches": sorted(
                    route.template for route in API_ROUTES if route_defs._matches_route_shape(path, route)
                ),
            }
            for name, path in SHAPES
        ],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['requests'])} requests)")


if __name__ == "__main__":
    main()
