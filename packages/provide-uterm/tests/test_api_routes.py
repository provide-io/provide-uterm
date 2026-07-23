#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the framework-neutral shared HTTP API route contract."""

from __future__ import annotations

import pytest
from provide.uterm.api_routes import API_ROUTE_REGISTRY, API_ROUTES, HttpMethod, RouteDef, RouteRegistry, RouteScope


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


def test_shared_route_inventory_includes_required_operations() -> None:
    expected_routes = (
        ("sessions.list", "GET", "/api/sessions", "global", "sessions.list", ()),
        ("sessions.create", "POST", "/api/sessions", "global", "sessions.create", ()),
        ("sessions.bulk_delete", "DELETE", "/api/sessions", "global", "sessions.bulk_delete", ("admin",)),
        ("sessions.get", "GET", "/api/sessions/{session_id}", "session", "sessions.get", ()),
        ("sessions.update", "PATCH", "/api/sessions/{session_id}", "session", "sessions.update", ()),
        ("sessions.delete", "DELETE", "/api/sessions/{session_id}", "session", "sessions.delete", ()),
        ("sessions.connect", "POST", "/api/sessions/{session_id}/connect", "session", "sessions.connect", ()),
        ("sessions.disconnect", "POST", "/api/sessions/{session_id}/disconnect", "session", "sessions.disconnect", ()),
        ("sessions.restart", "POST", "/api/sessions/{session_id}/restart", "session", "sessions.restart", ()),
        ("sessions.set_mode", "POST", "/api/sessions/{session_id}/mode", "session", "sessions.set_mode", ()),
        ("sessions.clear", "POST", "/api/sessions/{session_id}/clear", "session", "sessions.clear", ()),
        ("sessions.annotate", "POST", "/api/sessions/{session_id}/annotate", "session", "sessions.annotate", ()),
        ("sessions.analyze", "POST", "/api/sessions/{session_id}/analyze", "session", "sessions.analyze", ()),
        ("sessions.snapshot", "GET", "/api/sessions/{session_id}/snapshot", "session", "sessions.snapshot", ()),
        ("sessions.events", "GET", "/api/sessions/{session_id}/events", "session", "sessions.events", ()),
        (
            "sessions.events_watch",
            "GET",
            "/api/sessions/{session_id}/events/watch",
            "session",
            "sessions.events_watch",
            (),
        ),
        (
            "sessions.events_stream",
            "GET",
            "/api/sessions/{session_id}/events/stream",
            "session",
            "sessions.events_stream",
            (),
        ),
        ("sessions.recording", "GET", "/api/sessions/{session_id}/recording", "session", "sessions.recording", ()),
        (
            "sessions.recording_entries",
            "GET",
            "/api/sessions/{session_id}/recording/entries",
            "session",
            "sessions.recording_entries",
            (),
        ),
        (
            "sessions.recording_download",
            "GET",
            "/api/sessions/{session_id}/recording/download",
            "session",
            "sessions.recording_download",
            (),
        ),
        (
            "sessions.webhooks.create",
            "POST",
            "/api/sessions/{session_id}/webhooks",
            "session",
            "sessions.webhooks.create",
            (),
        ),
        (
            "sessions.webhooks.list",
            "GET",
            "/api/sessions/{session_id}/webhooks",
            "session",
            "sessions.webhooks.list",
            (),
        ),
        (
            "sessions.webhooks.delete",
            "DELETE",
            "/api/sessions/{session_id}/webhooks/{webhook_id}",
            "session",
            "sessions.webhooks.delete",
            (),
        ),
        ("tunnels.connect", "POST", "/api/connect", "global", "tunnels.connect", ()),
        ("tunnels.create", "POST", "/api/tunnels", "global", "tunnels.create", ()),
        ("tunnels.revoke_token", "DELETE", "/api/tunnels/{tunnel_id}/tokens", "global", "tunnels.revoke_token", ()),
        (
            "tunnels.rotate_token",
            "POST",
            "/api/tunnels/{tunnel_id}/tokens/rotate",
            "global",
            "tunnels.rotate_token",
            (),
        ),
        ("pam_events.ingest", "POST", "/api/pam-events", "global", "pam_events.ingest", ("operator", "admin")),
        ("profiles.list", "GET", "/api/profiles", "global", "profiles.list", ()),
        ("profiles.create", "POST", "/api/profiles", "global", "profiles.create", ()),
        ("profiles.get", "GET", "/api/profiles/{profile_id}", "global", "profiles.get", ()),
        ("profiles.update", "PUT", "/api/profiles/{profile_id}", "global", "profiles.update", ()),
        ("profiles.delete", "DELETE", "/api/profiles/{profile_id}", "global", "profiles.delete", ()),
        ("profiles.connect", "POST", "/api/profiles/{profile_id}/connect", "global", "profiles.connect", ()),
    )

    actual_routes = tuple(
        (route.operation, route.method.value, route.template, route.scope.value, route.capability, route.roles)
        for route in API_ROUTE_REGISTRY.routes
    )
    assert actual_routes == expected_routes
    assert API_ROUTE_REGISTRY.routes == API_ROUTES
