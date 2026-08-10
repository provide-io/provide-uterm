#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing suite for ``routes/route_defs.py``.

The shared binder every RouteDef family goes through, so its two guards apply
to the whole API surface at once:

* **``_route_guard`` re-validates the path against the shared grammar** and
  answers 422 when the concrete request does not match the RouteDef it was
  bound for. FastAPI already routed the request by then — this catches the case
  where the shared registry and the framework disagree about what the path
  means, which is exactly when a path parameter could carry something the
  registry would have rejected.
* **It then enforces the RouteDef's declared roles**, awaiting the authorizer if
  it is a coroutine function. A mutation that skips the await leaves a
  truthy coroutine object in ``authorized`` and authorizes everybody.

``bind_api_routes`` itself refuses to bind at all rather than half-registering:
an unknown RouteDef or a role-bearing route with no authorizer raises before
the router is touched.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import APIRouter, HTTPException

from provide.uterm.api_routes import API_ROUTES, HttpMethod, RouteDef
from provide.uterm.server.routes.route_defs import _route_guard, bind_api_routes

MODULE = "provide.uterm.server.routes.route_defs"


def _route(*, with_roles: bool = False) -> RouteDef:
    """A real shared RouteDef, with or without declared roles."""
    for route in API_ROUTES:
        if bool(route.roles) is with_roles:
            return route
    raise AssertionError(f"no shared route with roles={with_roles}")


async def _handler() -> dict[str, str]:
    return {}


def _handlers_for(*routes: RouteDef) -> dict[str, Any]:
    """A complete capability map — the registry validates the whole inventory."""
    return {route.capability: _handler for route in API_ROUTES}


def _request_for(route: RouteDef, path: str | None = None) -> MagicMock:
    req = MagicMock(name="request")
    req.url = MagicMock()
    req.url.path = path if path is not None else route.template
    return req


# ===========================================================================
# bind_api_routes — refusal before mutation
# ===========================================================================


class TestBindRefusals:
    def test_a_route_outside_the_shared_inventory_is_refused(self) -> None:
        stranger = RouteDef(
            operation="madeUp",
            method=HttpMethod.GET,
            template="/api/made-up",
            scope=_route().scope,
            capability="made.up",
            roles=(),
        )
        router = APIRouter()

        with pytest.raises(ValueError) as exc:
            bind_api_routes(router, _handlers_for(), (stranger,))

        assert str(exc.value) == "route definition is not in API_ROUTES"
        assert router.routes == []

    def test_a_role_bearing_route_without_an_authorizer_is_refused(self) -> None:
        """Binding it unguarded would serve a privileged route to anybody."""
        route = _route(with_roles=True)
        router = APIRouter()

        with pytest.raises(ValueError) as exc:
            bind_api_routes(router, _handlers_for(route), (route,))

        assert str(exc.value) == "role_authorizer is required for routes with required roles"
        assert router.routes == []

    def test_a_role_bearing_route_binds_when_an_authorizer_is_supplied(self) -> None:
        route = _route(with_roles=True)
        router = APIRouter()

        bind_api_routes(router, _handlers_for(route), (route,), role_authorizer=AsyncMock(return_value=True))

        assert len(router.routes) == 1

    def test_a_route_without_roles_needs_no_authorizer(self) -> None:
        route = _route(with_roles=False)
        router = APIRouter()

        bind_api_routes(router, _handlers_for(route), (route,))

        assert len(router.routes) == 1

    def test_the_capability_inventory_is_validated_before_binding(self) -> None:
        """An incomplete adapter must not leave the router partly registered."""
        route = _route(with_roles=False)
        router = APIRouter()

        with pytest.raises(Exception):  # the registry chooses the exception type
            bind_api_routes(router, {route.capability: _handler}, (route,))

        assert router.routes == []


class TestBindRouteWiring:
    def test_each_route_is_bound_at_its_template_with_its_own_verb(self) -> None:
        route = _route(with_roles=False)
        router = APIRouter()

        bind_api_routes(router, _handlers_for(route), (route,))

        bound = router.routes[0]
        assert bound.path == route.template  # type: ignore[attr-defined]
        assert bound.methods == {route.method.value}  # type: ignore[attr-defined]

    def test_the_operation_names_the_route_for_the_schema(self) -> None:
        route = _route(with_roles=False)
        router = APIRouter()

        bind_api_routes(router, _handlers_for(route), (route,))

        bound = router.routes[0]
        assert bound.name == route.operation  # type: ignore[attr-defined]
        assert bound.operation_id == route.operation  # type: ignore[attr-defined]

    def test_every_bound_route_carries_a_guard_dependency(self) -> None:
        route = _route(with_roles=False)
        router = APIRouter()

        bind_api_routes(router, _handlers_for(route), (route,))

        assert len(router.routes[0].dependencies) == 1  # type: ignore[attr-defined]

    async def test_the_bound_guard_carries_the_authorizer_through(self) -> None:
        """Binding the guard with a null authorizer silently disables role
        enforcement on a route that declares roles — the dependency is still
        attached, so nothing looks wrong from the outside. Execute it."""
        route = _route(with_roles=True)
        router = APIRouter()

        bind_api_routes(router, _handlers_for(route), (route,), role_authorizer=AsyncMock(return_value=False))

        guard = router.routes[0].dependencies[0].dependency  # type: ignore[attr-defined]
        match = MagicMock()
        match.route = route
        with patch(f"{MODULE}.API_ROUTE_REGISTRY") as registry:
            registry.match.return_value = match
            with pytest.raises(HTTPException) as exc:
                await guard(_request_for(route))

        assert exc.value.status_code == 403

    def test_the_handler_bound_is_the_one_for_that_capability(self) -> None:
        route = _route(with_roles=False)
        marker = AsyncMock(return_value={})
        handlers = _handlers_for(route)
        handlers[route.capability] = marker
        router = APIRouter()

        bind_api_routes(router, handlers, (route,))

        assert router.routes[0].endpoint is marker  # type: ignore[attr-defined]


# ===========================================================================
# bind_api_routes — the 405 catch-all
# ===========================================================================


def _by_template() -> dict[str, list[RouteDef]]:
    grouped: dict[str, list[RouteDef]] = {}
    for route in API_ROUTES:
        grouped.setdefault(route.template, []).append(route)
    return grouped


def _templates_with_multiple_methods() -> tuple[str, tuple[RouteDef, ...]]:
    for template, routes in _by_template().items():
        if len(routes) >= 2:
            return template, tuple(routes)
    raise AssertionError("no shared template serves two methods")


def _template_serving_exactly(count: int) -> tuple[str, tuple[RouteDef, ...]]:
    for template, routes in _by_template().items():
        if len(routes) == count:
            return template, tuple(routes)
    raise AssertionError(f"no shared template serves exactly {count} methods")


def _single_then_multi() -> tuple[tuple[RouteDef, ...], str]:
    """A selection whose SINGLE-method template is iterated first.

    The catch-all loop walks ``{route.template for route in selected}`` — a set,
    so iteration order is hash order, not source order. ``continue`` vs
    ``break`` is only observable when a skipped (single-method) template comes
    before one that still needs its catch-all. Rather than assume an order,
    pick a pair whose real order exposes it; the set is built here exactly as
    the code builds it, so what this sees is what the code will see.
    """
    grouped = _by_template()
    singles = [t for t, routes in grouped.items() if len(routes) == 1]
    multis = [t for t, routes in grouped.items() if len(routes) >= 2]
    for single in singles:
        for multi in multis:
            if next(iter({single, multi})) == single:
                return (*grouped[single], *grouped[multi]), multi
    raise AssertionError("no single/multi template pair iterates single-first")


class TestMethodNotAllowedCatchAll:
    """Starlette's default 405 comes from the first partial match, so a template
    with several RouteDef handlers would advertise only that one verb in
    ``Allow``. The catch-all restores the full contract."""

    def test_a_single_method_template_gets_no_catch_all(self) -> None:
        route = _route(with_roles=False)
        router = APIRouter()

        bind_api_routes(router, _handlers_for(route), (route,))

        assert len(router.routes) == 1

    def test_a_multi_method_template_gets_exactly_one_catch_all(self) -> None:
        template, routes = _templates_with_multiple_methods()
        authorizer = AsyncMock(return_value=True)
        router = APIRouter()

        bind_api_routes(router, _handlers_for(*routes), routes, role_authorizer=authorizer)

        assert len(router.routes) == len(routes) + 1

    async def test_the_catch_all_lists_every_bound_verb_sorted(self) -> None:
        template, routes = _templates_with_multiple_methods()
        authorizer = AsyncMock(return_value=True)
        router = APIRouter()

        bind_api_routes(router, _handlers_for(*routes), routes, role_authorizer=authorizer)

        catch_all = router.routes[-1]
        response = await catch_all.endpoint(MagicMock())  # type: ignore[attr-defined]
        expected = ", ".join(sorted(route.method.value for route in routes))
        assert response.headers["Allow"] == expected
        assert response.status_code == 405

    async def test_the_catch_all_body_says_method_not_allowed(self) -> None:
        _template, routes = _templates_with_multiple_methods()
        router = APIRouter()

        bind_api_routes(router, _handlers_for(*routes), routes, role_authorizer=AsyncMock(return_value=True))

        response = await router.routes[-1].endpoint(MagicMock())  # type: ignore[attr-defined]
        assert response.body == b'{"detail":"Method Not Allowed"}'

    def test_the_catch_all_answers_every_http_verb(self) -> None:
        """It exists to own the 405, so it must match the verbs nobody bound."""
        _template, routes = _templates_with_multiple_methods()
        router = APIRouter()

        bind_api_routes(router, _handlers_for(*routes), routes, role_authorizer=AsyncMock(return_value=True))

        assert router.routes[-1].methods == {m.value for m in HttpMethod}  # type: ignore[attr-defined]

    def test_a_template_serving_exactly_two_methods_gets_a_catch_all(self) -> None:
        """The threshold is ``< 2``, so two methods is already enough. Picking a
        3-method template to test with hides both off-by-one mutations."""
        _template, routes = _template_serving_exactly(2)
        router = APIRouter()

        bind_api_routes(router, _handlers_for(*routes), routes, role_authorizer=AsyncMock(return_value=True))

        assert len(router.routes) == 3

    def test_a_skipped_template_does_not_abandon_the_remaining_ones(self) -> None:
        """``continue``, not ``break``: a single-method template in the middle
        of the sweep must not cost every later template its catch-all."""
        routes, multi_template = _single_then_multi()
        router = APIRouter()

        bind_api_routes(router, _handlers_for(*routes), routes, role_authorizer=AsyncMock(return_value=True))

        catch_alls = [r for r in router.routes if getattr(r, "include_in_schema", True) is False]
        assert [r.path for r in catch_alls] == [multi_template]  # type: ignore[attr-defined]

    def test_the_catch_all_stays_out_of_the_schema(self) -> None:
        _template, routes = _templates_with_multiple_methods()
        router = APIRouter()

        bind_api_routes(router, _handlers_for(*routes), routes, role_authorizer=AsyncMock(return_value=True))

        assert router.routes[-1].include_in_schema is False  # type: ignore[attr-defined]


# ===========================================================================
# _route_guard — path grammar
# ===========================================================================


class TestRouteGuardPathGrammar:
    async def test_a_path_matching_the_route_passes(self) -> None:
        route = _route(with_roles=False)
        guard = _route_guard(route, None)
        match = MagicMock()
        match.route = route

        with patch(f"{MODULE}.API_ROUTE_REGISTRY") as registry:
            registry.match.return_value = match
            assert await guard(_request_for(route)) is None

    async def test_an_unmatched_path_is_a_422(self) -> None:
        route = _route(with_roles=False)
        guard = _route_guard(route, None)

        with patch(f"{MODULE}.API_ROUTE_REGISTRY") as registry:
            registry.match.return_value = None
            with pytest.raises(HTTPException) as exc:
                await guard(_request_for(route))

        assert exc.value.status_code == 422
        assert exc.value.detail == "invalid route path parameters"

    async def test_a_path_matching_a_different_route_is_a_422(self) -> None:
        """Identity, not truthiness: the registry may match some other RouteDef
        on the same path shape, which is not the one this guard was built for."""
        route = _route(with_roles=False)
        guard = _route_guard(route, None)
        match = MagicMock()
        match.route = RouteDef(
            operation="other",
            method=route.method,
            template=route.template,
            scope=route.scope,
            capability="other.capability",
            roles=(),
        )

        with patch(f"{MODULE}.API_ROUTE_REGISTRY") as registry:
            registry.match.return_value = match
            with pytest.raises(HTTPException) as exc:
                await guard(_request_for(route))

        assert exc.value.status_code == 422

    async def test_the_registry_is_asked_about_this_method_and_the_live_path(self) -> None:
        route = _route(with_roles=False)
        guard = _route_guard(route, None)
        match = MagicMock()
        match.route = route
        req = _request_for(route, path="/api/live/path")

        with patch(f"{MODULE}.API_ROUTE_REGISTRY") as registry:
            registry.match.return_value = match
            await guard(req)

        registry.match.assert_called_once_with(route.method, "/api/live/path")


# ===========================================================================
# _route_guard — role enforcement
# ===========================================================================


class TestRouteGuardRoles:
    async def test_an_authorized_principal_passes(self) -> None:
        route = _route(with_roles=True)
        authorizer = AsyncMock(return_value=True)
        guard = _route_guard(route, authorizer)
        match = MagicMock()
        match.route = route

        with patch(f"{MODULE}.API_ROUTE_REGISTRY") as registry:
            registry.match.return_value = match
            assert await guard(_request_for(route)) is None

    async def test_an_unauthorized_principal_is_a_403(self) -> None:
        route = _route(with_roles=True)
        guard = _route_guard(route, AsyncMock(return_value=False))
        match = MagicMock()
        match.route = route

        with patch(f"{MODULE}.API_ROUTE_REGISTRY") as registry:
            registry.match.return_value = match
            with pytest.raises(HTTPException) as exc:
                await guard(_request_for(route))

        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient role privileges"

    async def test_the_authorizer_receives_the_request_and_the_declared_roles(self) -> None:
        route = _route(with_roles=True)
        authorizer = AsyncMock(return_value=True)
        guard = _route_guard(route, authorizer)
        match = MagicMock()
        match.route = route
        req = _request_for(route)

        with patch(f"{MODULE}.API_ROUTE_REGISTRY") as registry:
            registry.match.return_value = match
            await guard(req)

        authorizer.assert_awaited_once_with(req, route.roles)

    async def test_a_synchronous_authorizer_is_used_without_awaiting(self) -> None:
        """``inspect.isawaitable`` — the contract allows either flavour."""
        route = _route(with_roles=True)
        guard = _route_guard(route, MagicMock(return_value=False))
        match = MagicMock()
        match.route = route

        with patch(f"{MODULE}.API_ROUTE_REGISTRY") as registry:
            registry.match.return_value = match
            with pytest.raises(HTTPException) as exc:
                await guard(_request_for(route))

        assert exc.value.status_code == 403

    async def test_a_coroutine_result_is_awaited_before_being_believed(self) -> None:
        """Skipping the await leaves a coroutine object in `authorized`, which
        is truthy — every caller would be authorized regardless of the answer."""
        route = _route(with_roles=True)
        guard = _route_guard(route, AsyncMock(return_value=False))
        match = MagicMock()
        match.route = route

        with patch(f"{MODULE}.API_ROUTE_REGISTRY") as registry:
            registry.match.return_value = match
            with pytest.raises(HTTPException):
                await guard(_request_for(route))

    async def test_a_route_without_roles_never_consults_the_authorizer(self) -> None:
        route = _route(with_roles=False)
        authorizer = AsyncMock(return_value=False)
        guard = _route_guard(route, authorizer)
        match = MagicMock()
        match.route = route

        with patch(f"{MODULE}.API_ROUTE_REGISTRY") as registry:
            registry.match.return_value = match
            assert await guard(_request_for(route)) is None

        authorizer.assert_not_awaited()

    async def test_roles_are_not_enforced_when_no_authorizer_was_supplied(self) -> None:
        """bind_api_routes refuses that combination, so reaching here means the
        guard was built directly; it must not crash on the missing authorizer."""
        route = _route(with_roles=True)
        guard = _route_guard(route, None)
        match = MagicMock()
        match.route = route

        with patch(f"{MODULE}.API_ROUTE_REGISTRY") as registry:
            registry.match.return_value = match
            assert await guard(_request_for(route)) is None
