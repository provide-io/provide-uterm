#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the shared API route registry.

The inventory of HTTP operations both backends serve, with no framework in
it. The FastAPI server and the Cloudflare Worker each dispatch from this same
table, which is the only reason a client can talk to either without knowing
which it reached.

**Templates are validated at construction, not at request time.** A malformed
one is a programming error, and finding it when the table is built means it
cannot ship. Only four parameter names exist, and a segment naming anything
else is refused rather than matched as a literal.

**Overlapping routes are refused outright.** Two templates that could match
the same path would make dispatch depend on declaration order — the registry
raises instead, so no route can ever be shadowed by another. The check is
narrower than it looks: a static segment that no parameter value could equal
does not intersect a parameter, because the parameter pattern is stricter
than what a static segment may contain.

**A parameter matches one segment of a restricted alphabet.** Not a catch-all
— a path with a slash, a dot or an empty segment where a parameter goes is
not a match with a strange value, it is no match at all, which is what makes
the 404-versus-422 distinction downstream possible.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_apiroutes_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.api_routes import (
    API_ROUTE_REGISTRY,
    API_ROUTES,
    HttpMethod,
    RouteDef,
    RouteRegistry,
    RouteScope,
)

OUT = Path(__file__).with_name("apiroutes_golden.json")


def _route(route: RouteDef) -> dict[str, Any]:
    """One route as JSON carries it."""
    return {
        "operation": route.operation,
        "method": route.method.value,
        "template": route.template,
        "scope": route.scope.value,
        "capability": route.capability,
        "roles": list(route.roles),
    }


def _error(call: Any) -> dict[str, Any]:
    """What a construction refuses, and how it says so."""
    try:
        call()
    except (ValueError, TypeError) as exc:
        return {"error": type(exc).__name__, "message": str(exc)}
    return {"error": None, "message": None}


# (method, path) — what dispatch resolves to.
MATCH_CASES: list[tuple[str, str]] = [
    ("GET", "/api/sessions"),
    ("POST", "/api/sessions"),
    ("DELETE", "/api/sessions"),
    ("PATCH", "/api/sessions"),
    ("GET", "/api/sessions/w1"),
    ("PATCH", "/api/sessions/w1"),
    ("DELETE", "/api/sessions/w1"),
    ("POST", "/api/sessions/w1/connect"),
    ("GET", "/api/sessions/w1/events/stream"),
    ("GET", "/api/sessions/w1/recording/download"),
    ("DELETE", "/api/sessions/w1/webhooks/wh1"),
    ("POST", "/api/tunnels/t1/tokens/rotate"),
    ("GET", "/api/profiles/p1"),
    ("PUT", "/api/profiles/p1"),
    ("POST", "/api/pam-events"),
    # Method matching is case-insensitive on the way in.
    ("get", "/api/sessions"),
    ("GeT", "/api/sessions"),
    # A method the contract has no route for, and one that is not a method.
    ("HEAD", "/api/sessions"),
    ("OPTIONS", "/api/sessions"),
    ("BREW", "/api/sessions"),
    ("", "/api/sessions"),
    # Paths that must not match.
    ("GET", "/api/sessions/"),
    ("GET", "/api/sessions//events"),
    ("GET", "/api/nope"),
    ("GET", "/api"),
    ("GET", "/"),
    ("GET", ""),
    ("GET", "api/sessions"),
    ("GET", "/API/sessions"),
    ("GET", "/api/sessions/w1/"),
    ("GET", "/api/sessions/w1/nope"),
    # A parameter is one segment of a restricted alphabet.
    ("GET", "/api/sessions/a.b"),
    ("GET", "/api/sessions/a~b"),
    ("GET", "/api/sessions/a b"),
    ("GET", "/api/sessions/a%2Fb"),
    ("GET", "/api/sessions/a/b"),
    ("GET", "/api/sessions/" + "x" * 64),
    ("GET", "/api/sessions/" + "x" * 65),
    ("GET", "/api/sessions/w-1_2"),
    # A newline, which an unanchored or multiline matcher would let through.
    ("GET", "/api/sessions/w1\n"),
    ("GET", "/api/sessions/w1\nx"),
    ("GET", "\n/api/sessions/w1"),
    # Unicode digits, which are not the ASCII alphabet the parameter allows.
    ("GET", "/api/sessions/٣"),
    ("GET", "/api/sessions/ｗ1"),  # noqa: RUF001 - a full-width w, which is the point
]

# Paths whose allowed methods a 405 would report.
ALLOWED_CASES: list[str] = [
    "/api/sessions",
    "/api/sessions/w1",
    "/api/sessions/w1/connect",
    "/api/sessions/w1/webhooks",
    "/api/sessions/w1/webhooks/wh1",
    "/api/tunnels/t1/tokens",
    "/api/profiles/p1",
    "/api/profiles",
    "/api/nope",
    "/api/sessions/",
    "/api/sessions/a.b",
]

# Templates a registry refuses, with a valid one for contrast.
TEMPLATE_CASES: list[tuple[str, str]] = [
    ("a valid template", "/api/things"),
    ("a valid parameter", "/api/things/{session_id}"),
    ("no leading /api", "/things"),
    ("a bare path", "/api"),
    ("nothing at all", ""),
    ("a trailing slash", "/api/things/"),
    ("an empty segment", "/api//things"),
    ("a query string", "/api/things?x=1"),
    ("a fragment", "/api/things#x"),
    ("an unknown parameter name", "/api/things/{thing_id}"),
    ("a repeated parameter", "/api/{session_id}/{session_id}"),
    ("a brace inside a segment", "/api/th{ing}s"),
    ("an unclosed brace", "/api/{session_id"),
    ("an unopened brace", "/api/session_id}"),
    ("a static segment with a slash escape", "/api/th%2Fings"),
    ("a static segment with a space", "/api/th ings"),
    ("a static segment of punctuation", "/api/things!"),
    # The alphabet a static segment may use, which is wider than a
    # parameter's.
    ("a static segment with a dot", "/api/things.json"),
    ("a static segment with a tilde", "/api/al~pha"),
    ("a static segment with a dash", "/api/al-pha"),
    ("a static segment with an underscore", "/api/al_pha"),
]

# (name, first, second) — templates that may or may not collide.
INTERSECT_CASES: list[tuple[str, str, str]] = [
    ("a parameter against a plain segment", "/api/things/{session_id}", "/api/things/latest"),
    ("two parameters", "/api/things/{session_id}", "/api/things/{tunnel_id}"),
    ("different lengths", "/api/things/{session_id}", "/api/things"),
    ("different static segments", "/api/things", "/api/others"),
    # A static segment no parameter value could equal does not collide, since
    # the parameter alphabet is stricter than a static segment's.
    ("a parameter against a dotted segment", "/api/things/{session_id}", "/api/things/all.json"),
    ("a parameter against a tilde segment", "/api/things/{session_id}", "/api/al~pha/x"),
    ("a parameter in the middle", "/api/{session_id}/x", "/api/things/x"),
    # The same pairs the other way round. Which side holds the parameter
    # decides which arm of the comparison runs, and a port that only ever
    # sees one order leaves the other untested.
    ("a plain segment declared before a parameter", "/api/things/latest", "/api/things/{session_id}"),
    ("a dotted segment declared before a parameter", "/api/things/all.json", "/api/things/{session_id}"),
    ("a parameter against a longer static segment", "/api/things/{session_id}", "/api/things/" + "x" * 65),
    ("identical templates", "/api/things", "/api/things"),
    # Two static segments differing only in case are two different paths, so
    # a case-insensitive comparison would refuse a legitimate pair.
    ("static segments differing in case", "/api/Things", "/api/things"),
]

# A registry of its own, to exercise what the shared table has no example of:
# static segments using the wider alphabet.
CUSTOM_TEMPLATES: list[str] = [
    "/api/things.json",
    "/api/al~pha",
    "/api/al-pha",
    "/api/things/{session_id}",
]

# Paths against that registry. A dot compiled unescaped would match any
# character, so `/api/thingsxjson` would reach the route for `/api/things.json`.
CUSTOM_PATHS: list[str] = [
    "/api/things.json",
    "/api/thingsxjson",
    "/api/things/json",
    "/api/al~pha",
    "/api/alxpha",
    "/api/al-pha",
    "/api/al_pha",
    "/api/things/w1",
]


def _pair(first: str, second: str) -> Any:
    """Two routes sharing a method, which the registry may refuse."""
    return lambda: RouteRegistry(
        (
            RouteDef("a", HttpMethod.GET, first, RouteScope.GLOBAL, "a", ()),
            RouteDef("b", HttpMethod.GET, second, RouteScope.GLOBAL, "b", ()),
        )
    )


def _build() -> dict[str, Any]:
    """Everything the registry decides."""
    matches = []
    for method, path in MATCH_CASES:
        found = API_ROUTE_REGISTRY.match(method, path)
        matches.append(
            {
                "method": method,
                "path": path,
                "operation": None if found is None else found.route.operation,
                "params": None if found is None else dict(found.params),
            }
        )

    return {
        "methods": [method.value for method in HttpMethod],
        "scopes": [scope.value for scope in RouteScope],
        "routes": [_route(route) for route in API_ROUTES],
        "capabilities": sorted({route.capability for route in API_ROUTES}),
        "matches": matches,
        "allowed": [
            {"path": path, "methods": [method.value for method in API_ROUTE_REGISTRY.allowed_methods(path)]}
            for path in ALLOWED_CASES
        ],
        "templates": [
            {
                "name": name,
                "template": template,
                **_error(
                    lambda t=template: RouteRegistry(  # type: ignore[misc]
                        (RouteDef("a", HttpMethod.GET, t, RouteScope.GLOBAL, "a", ()),)
                    )
                ),
            }
            for name, template in TEMPLATE_CASES
        ],
        "intersections": [
            {"name": name, "first": first, "second": second, **_error(_pair(first, second))}
            for name, first, second in INTERSECT_CASES
        ],
        # A session route must be able to say which session it is.
        "session_without_parameter": _error(
            lambda: RouteRegistry((RouteDef("a", HttpMethod.GET, "/api/things", RouteScope.SESSION, "a", ()),))
        ),
        # Another parameter is not the one it needs: a session route has to be
        # able to say which session it acts on, and a port checking merely
        # that some parameter exists would accept this.
        "session_with_wrong_parameter": _error(
            lambda: RouteRegistry(
                (RouteDef("a", HttpMethod.GET, "/api/things/{tunnel_id}", RouteScope.SESSION, "a", ()),)
            )
        ),
        "custom_templates": CUSTOM_TEMPLATES,
        "custom_matches": [
            {
                "path": path,
                "operation": None if found is None else found.route.operation,
                "params": None if found is None else dict(found.params),
            }
            for path in CUSTOM_PATHS
            for found in (
                RouteRegistry(
                    tuple(
                        RouteDef(f"r{index}", HttpMethod.GET, template, RouteScope.GLOBAL, f"r{index}", ())
                        for index, template in enumerate(CUSTOM_TEMPLATES)
                    )
                ).match("GET", path),
            )
        ],
        "session_with_parameter": _error(
            lambda: RouteRegistry(
                (RouteDef("a", HttpMethod.GET, "/api/things/{session_id}", RouteScope.SESSION, "a", ()),)
            )
        ),
        # Metadata that has to stay stable, because a backend keys off it.
        "blank_operation": _error(
            lambda: RouteRegistry((RouteDef("", HttpMethod.GET, "/api/things", RouteScope.GLOBAL, "a", ()),))
        ),
        "padded_operation": _error(
            lambda: RouteRegistry((RouteDef(" a ", HttpMethod.GET, "/api/things", RouteScope.GLOBAL, "a", ()),))
        ),
        "blank_capability": _error(
            lambda: RouteRegistry((RouteDef("a", HttpMethod.GET, "/api/things", RouteScope.GLOBAL, "", ()),))
        ),
        "padded_capability": _error(
            lambda: RouteRegistry((RouteDef("a", HttpMethod.GET, "/api/things", RouteScope.GLOBAL, "a\t", ()),))
        ),
        # The same method and template twice, which no dispatch could resolve.
        "duplicate": _error(
            lambda: RouteRegistry(
                (
                    RouteDef("a", HttpMethod.GET, "/api/things", RouteScope.GLOBAL, "a", ()),
                    RouteDef("b", HttpMethod.GET, "/api/things", RouteScope.GLOBAL, "b", ()),
                )
            )
        ),
        # The same template under two methods, which is the ordinary case.
        "same_template_two_methods": _error(
            lambda: RouteRegistry(
                (
                    RouteDef("a", HttpMethod.GET, "/api/things", RouteScope.GLOBAL, "a", ()),
                    RouteDef("b", HttpMethod.POST, "/api/things", RouteScope.GLOBAL, "b", ()),
                )
            )
        ),
        "empty_registry": _error(lambda: RouteRegistry(())),
        # A backend that cannot serve every route in the table.
        "capabilities_all": _error(lambda: API_ROUTE_REGISTRY.validate_capabilities(r.capability for r in API_ROUTES)),
        "capabilities_missing": _error(lambda: API_ROUTE_REGISTRY.validate_capabilities(["sessions.list"])),
        "capabilities_none": _error(lambda: API_ROUTE_REGISTRY.validate_capabilities([])),
        "capabilities_extra": _error(
            lambda: API_ROUTE_REGISTRY.validate_capabilities([r.capability for r in API_ROUTES] + ["extra"])
        ),
        # The roles field is normalised to a tuple whatever it arrives as.
        "roles_from_frozenset": list(
            RouteDef("a", HttpMethod.GET, "/api/things", RouteScope.GLOBAL, "a", frozenset({"admin"})).roles
        ),
        "roles_from_list": list(
            RouteDef("a", HttpMethod.GET, "/api/things", RouteScope.GLOBAL, "a", ["admin", "operator"]).roles  # type: ignore[arg-type]
        ),
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(API_ROUTES)} routes, {len(MATCH_CASES)} matches, {len(TEMPLATE_CASES)} templates)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
