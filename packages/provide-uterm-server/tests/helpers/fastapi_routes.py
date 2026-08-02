#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Route introspection helpers for FastAPI >= 0.141.

Up to FastAPI 0.136, ``app.include_router(...)`` *flattened* the included
router: every sub-route was rebuilt with the joined path and the merged
dependency list and appended to ``app.routes``.  Tests could therefore treat
``app.routes`` as "the effective routing table" and read ``.path`` /
``.methods`` / ``.dependencies`` straight off each entry.

FastAPI 0.141 stopped doing that.  An include now contributes a *single*
``fastapi.routing._IncludedRouter`` entry to ``app.routes``, and the effective
routes underneath it are materialised lazily (and recursively, for nested
includes).  Iterating ``app.routes`` directly therefore yields router objects
with no ``.path``.

``fastapi.routing.iter_route_contexts()`` is the public accessor FastAPI added
for exactly this: it expands ``_IncludedRouter`` entries and yields one
``fastapi.routing.RouteContext`` per effective route, in matching order.
:func:`iter_effective_routes` wraps it so the next FastAPI shape change is a
one-line edit here rather than an edit in every test module.

Note: ``tests/server/test_routes_mutation_killing.py`` deliberately does *not*
import this module — it is copied into mutmut's ``mutants/`` tree, which only
receives ``tests/{bridge,server,tunnel}`` (see ``also_copy`` in the root
``pyproject.toml``), so ``tests.helpers`` is not importable there.  That suite
uses ``iter_route_contexts`` directly; it only inspects HTTP ``APIRoute``
entries, so it needs none of the WebSocket handling below.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from fastapi.routing import APIRoute, iter_route_contexts


def _routes_of(source: Any) -> Sequence[Any]:
    """Accept a ``FastAPI`` app, an ``APIRouter``, or a plain route sequence."""
    routes = getattr(source, "routes", None)
    return source if routes is None else routes


def iter_effective_routes(source: Any) -> Iterator[Any]:
    """Yield one object per *effective* route reachable from *source*.

    Each yielded object exposes the post-include state — the joined ``path``,
    the merged ``dependencies``/``dependant``, ``methods`` and ``endpoint`` —
    i.e. exactly what ``app.routes`` used to contain before FastAPI 0.141.
    """
    for context in iter_route_contexts(_routes_of(source)):
        # FastAPI writes the effective state onto the context itself for
        # APIRoute entries, but for WebSocket/Mount/plain-Route entries it
        # instead rebuilds a Starlette route in ``starlette_route`` and leaves
        # ``context.path`` empty.  Prefer the rebuilt route whenever one
        # exists so ``.path`` and ``.dependant`` are correct for every kind.
        rebuilt = getattr(context, "starlette_route", None)
        yield context if rebuilt is None else rebuilt


def effective_route_paths(source: Any) -> set[str]:
    """Return the set of effective paths reachable from *source*."""
    paths = (getattr(route, "path", None) for route in iter_effective_routes(source))
    return {path for path in paths if path is not None}


def find_effective_api_route(source: Any, path: str) -> Any | None:
    """Return the effective state of the first ``APIRoute`` registered at *path*.

    Returns ``None`` when no HTTP route is registered at that exact path.  The
    returned object carries the *merged* dependency list (router-level
    ``include_router(dependencies=...)`` entries first, then route-level ones),
    so callers can assert on the dependencies that actually run for a request.
    """
    for route in iter_effective_routes(source):
        original = getattr(route, "original_route", route)
        if isinstance(original, APIRoute) and getattr(route, "path", None) == path:
            return route
    return None
