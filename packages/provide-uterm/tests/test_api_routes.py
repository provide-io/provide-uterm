#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the framework-neutral shared HTTP API route contract."""

from __future__ import annotations

import pytest
from provide.uterm.api_routes import HttpMethod, RouteDef, RouteRegistry, RouteScope


def _route(
    operation: str = "get_session",
    method: HttpMethod = HttpMethod.GET,
    template: str = "/api/sessions/{session_id}",
    scope: RouteScope = RouteScope.SESSION,
    capability: str = "session.read",
) -> RouteDef:
    return RouteDef(
        operation=operation,
        method=method,
        template=template,
        scope=scope,
        capability=capability,
        roles=("viewer",),
    )


def test_match_extracts_valid_named_path_parameters() -> None:
    registry = RouteRegistry((_route(),))

    match = registry.match(HttpMethod.GET, "/api/sessions/demo_123-abc")

    assert match is not None
    assert match.route.operation == "get_session"
    assert match.params == {"session_id": "demo_123-abc"}


def test_allowed_methods_are_sorted_for_a_matching_path() -> None:
    registry = RouteRegistry(
        (
            _route(operation="update_session", method=HttpMethod.PATCH, capability="session.write"),
            _route(operation="get_session", method=HttpMethod.GET),
            _route(operation="delete_session", method=HttpMethod.DELETE, capability="session.delete"),
        )
    )

    assert registry.allowed_methods("/api/sessions/demo") == (HttpMethod.DELETE, HttpMethod.GET, HttpMethod.PATCH)


def test_duplicate_method_and_template_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate route"):
        RouteRegistry((_route(), _route(operation="get_session_again")))


@pytest.mark.parametrize(
    "second_template",
    (
        "/api/things/{tunnel_id}",
        "/api/things/list",
    ),
)
def test_intersecting_same_method_templates_are_rejected(second_template: str) -> None:
    with pytest.raises(ValueError, match="intersecting route"):
        RouteRegistry(
            (
                _route(operation="get_thing", template="/api/things/{session_id}"),
                _route(
                    operation="get_other_thing",
                    template=second_template,
                    scope=RouteScope.GLOBAL,
                ),
            )
        )


@pytest.mark.parametrize(
    "template",
    (
        "api/sessions/{session_id}",
        "/health",
        "/api/sessions/{unknown_id}",
        "/api/sessions/{session_id}/",
    ),
)
def test_invalid_templates_are_rejected(template: str) -> None:
    with pytest.raises(ValueError, match="template"):
        RouteRegistry((_route(template=template),))


def test_session_scope_route_without_session_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="session_id"):
        RouteRegistry(
            (
                _route(
                    template="/api/tunnels/{tunnel_id}",
                ),
            )
        )


def test_invalid_parameter_value_does_not_match() -> None:
    registry = RouteRegistry((_route(),))

    assert registry.match(HttpMethod.GET, "/api/sessions/not.valid") is None
    assert registry.match(HttpMethod.GET, "/api/sessions/" + ("a" * 65)) is None


def test_missing_capability_is_rejected() -> None:
    registry = RouteRegistry((_route(capability="session.read"),))

    with pytest.raises(ValueError, match="session.read"):
        registry.validate_capabilities({"session.write"})


@pytest.mark.parametrize(
    ("field", "value"), (("operation", ""), ("operation", " "), ("capability", ""), ("capability", " "))
)
def test_blank_operation_or_capability_is_rejected(field: str, value: str) -> None:
    route_kwargs = {field: value}

    with pytest.raises(ValueError, match=field):
        RouteRegistry((_route(**route_kwargs),))
