#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-server
"""Generate the differential golden corpus for binding shared routes to handlers.

The shared table says which operations exist; this decides what happens when a
request arrives for one. It is the layer both backends need and the reason no
HTTP router is required: a method and a path go in, a handler or a refusal
comes out.

**A backend that cannot serve every route it selected is refused before
anything is registered.** An adapter half-registered is worse than one that
did not start: the routes that did bind would answer, and the ones that did
not would 404 as though they had never existed.

**A route with required roles needs an authorizer.** Selecting one without
supplying the check that guards it would publish it unguarded, which is the
one mistake this layer exists to prevent.

**A path that matches the template but not the grammar is a 422, not a 404.**
The route exists; the parameters are wrong. Telling them apart is the
difference between "you asked for something that is not here" and "you asked
wrongly".

**A path with more than one method reports them all.** A framework emitting
its own 405 from the first partial match would name only that one method in
``Allow``, so a client would retry with the wrong verb.

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_routebinding_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from provide.uterm.api_routes import API_ROUTES, HttpMethod, RouteDef, RouteScope

from provide.uterm.server.routes.route_defs import _route_guard, bind_api_routes

OUT = Path(__file__).with_name("routebinding_golden.json")


class _Router:
    """A router that records what was registered on it."""

    def __init__(self) -> None:
        self.registered: list[dict[str, Any]] = []

    def add_api_route(self, template: str, _handler: Any, **kwargs: Any) -> None:
        self.registered.append(
            {
                "template": template,
                "methods": list(kwargs.get("methods", [])),
                "name": kwargs.get("name"),
                "in_schema": kwargs.get("include_in_schema", True),
                "headers": None,
            }
        )


def _by_operation(*operations: str) -> tuple[RouteDef, ...]:
    """The shared routes named, in the table's own order."""
    wanted = set(operations)
    return tuple(route for route in API_ROUTES if route.operation in wanted)


def _handlers(_routes: tuple[RouteDef, ...]) -> dict[str, Any]:
    """A handler for every capability in the shared table.

    Not merely the selected routes': the reference validates against the whole
    inventory, so a backend that can serve only some of it is refused outright
    rather than registering the part it can.
    """
    return {route.capability: (lambda: None) for route in API_ROUTES}


def _error(call: Any) -> dict[str, Any]:
    """What a binding refuses, and how it says so."""
    try:
        call()
    except (ValueError, TypeError) as exc:
        return {"error": type(exc).__name__, "message": str(exc)}
    return {"error": None, "message": None}


def _bind(operations: tuple[str, ...], *, with_authorizer: bool = False, missing: tuple[str, ...] = ()) -> Any:
    """Bind the named operations, optionally dropping some handlers."""
    routes = _by_operation(*operations)
    handlers = _handlers(routes)
    for capability in missing:
        handlers.pop(capability, None)
    router = _Router()

    def run() -> None:
        bind_api_routes(
            router,
            handlers,
            routes,
            role_authorizer=(lambda _request, _roles: True) if with_authorizer else None,
        )

    return router, run


# (name, operations, with_authorizer, missing) — what a binding accepts.
BIND_CASES: list[tuple[str, tuple[str, ...], bool, tuple[str, ...]]] = [
    ("one route", ("sessions.get",), False, ()),
    ("two methods on one path", ("sessions.list", "sessions.create"), False, ()),
    ("three methods on one path", ("sessions.list", "sessions.create", "sessions.bulk_delete"), True, ()),
    ("routes on separate paths", ("sessions.get", "sessions.connect"), False, ()),
    ("nothing at all", (), False, ()),
    # A capability with no handler: refused before anything is registered.
    ("a handler missing for a selected route", ("sessions.get", "sessions.connect"), False, ("sessions.connect",)),
    # A capability the binding did not even select. Still refused: the check
    # is against the whole inventory, so a backend serving part of the
    # contract cannot register the part it has.
    ("a handler missing for a route not selected", ("sessions.get",), False, ("tunnels.create",)),
    ("several handlers missing", ("sessions.get",), False, ("tunnels.create", "profiles.list")),
    # A guarded route with nothing to guard it.
    ("a guarded route with no authorizer", ("sessions.bulk_delete",), False, ()),
    ("a guarded route with an authorizer", ("sessions.bulk_delete",), True, ()),
    ("an unguarded route with an authorizer", ("sessions.get",), True, ()),
]


def _foreign_route() -> Any:
    """A route that is not in the shared table at all."""
    route = RouteDef("made.up", HttpMethod.GET, "/api/made-up", RouteScope.GLOBAL, "made.up", ())
    router = _Router()
    return lambda: bind_api_routes(router, {"made.up": lambda: None}, (route,))


class _Url:
    """Just the path a guard reads."""

    def __init__(self, path: str) -> None:
        self.path = path


class _Request:
    """Just the URL a guard reads."""

    def __init__(self, path: str) -> None:
        self.url = _Url(path)


# (name, operation, path, authorized) — what the per-route guard decides.
GUARD_CASES: list[tuple[str, str, str, Any]] = [
    ("the route's own path", "sessions.get", "/api/sessions/w1", None),
    ("a path with a bad parameter", "sessions.get", "/api/sessions/a.b", None),
    ("a path with no parameter", "sessions.get", "/api/sessions", None),
    ("a path for another route", "sessions.get", "/api/sessions/w1/connect", None),
    ("a path that is not an api path", "sessions.get", "/nope", None),
    # A guarded route: the authorizer decides.
    ("a guarded route, allowed", "sessions.bulk_delete", "/api/sessions", True),
    ("a guarded route, refused", "sessions.bulk_delete", "/api/sessions", False),
    ("a guarded route on a bad path", "sessions.bulk_delete", "/api/sessions/", True),
    # An unguarded route ignores the authorizer entirely.
    ("an unguarded route with a refusing authorizer", "sessions.get", "/api/sessions/w1", False),
]


async def _guards() -> list[dict[str, Any]]:
    """What each guard decides, and with what status."""
    out = []
    for name, operation, path, authorized in GUARD_CASES:
        route = _by_operation(operation)[0]
        authorizer = None if authorized is None else (lambda _request, _roles, a=authorized: a)
        guard = _route_guard(route, authorizer)
        try:
            await guard(_Request(path))
            record: dict[str, Any] = {"status": None, "detail": None}
        except Exception as exc:
            record = {"status": getattr(exc, "status_code", None), "detail": getattr(exc, "detail", str(exc))}
        out.append({"name": name, "operation": operation, "path": path, "authorized": authorized, **record})
    return out


def _build() -> dict[str, Any]:
    """Everything binding decides."""
    bindings = []
    for name, operations, with_authorizer, missing in BIND_CASES:
        router, run = _bind(operations, with_authorizer=with_authorizer, missing=missing)
        outcome = _error(run)
        bindings.append(
            {
                "name": name,
                "operations": list(operations),
                "with_authorizer": with_authorizer,
                "missing": list(missing),
                "registered": router.registered,
                **outcome,
            }
        )

    return {
        "bindings": bindings,
        "foreign_route": _error(_foreign_route()),
        "guards": asyncio.run(_guards()),
        # Every method the catch-all is registered for, in the table's order.
        "all_methods": [method.value for method in HttpMethod],
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(BIND_CASES)} bindings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
