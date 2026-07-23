#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Framework-neutral definitions for the shared HTTP API surface."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

_PARAMETER_NAMES = frozenset({"session_id", "tunnel_id", "profile_id", "webhook_id"})
_PARAMETER_PATTERN = r"[A-Za-z0-9_-]{1,64}"
_STATIC_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9._~-]+")


class HttpMethod(StrEnum):
    """HTTP methods supported by the shared API contract."""

    DELETE = "DELETE"
    GET = "GET"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"
    PATCH = "PATCH"
    POST = "POST"
    PUT = "PUT"


class RouteScope(StrEnum):
    """Where a route executes."""

    GLOBAL = "global"
    SESSION = "session"


@dataclass(frozen=True, slots=True)
class RouteDef:
    """A single shared API operation without framework-specific behavior."""

    operation: str
    method: HttpMethod
    template: str
    scope: RouteScope
    capability: str
    roles: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", tuple(self.roles))


@dataclass(frozen=True, slots=True)
class RouteMatch:
    """A route definition and its immutable extracted path parameters."""

    route: RouteDef
    params: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))


@dataclass(frozen=True, slots=True)
class RouteRegistry:
    """Validated, immutable collection of route definitions."""

    routes: tuple[RouteDef, ...]
    _compiled_routes: tuple[tuple[RouteDef, re.Pattern[str]], ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        routes = tuple(self.routes)
        signatures: set[tuple[HttpMethod, str]] = set()
        compiled_routes: list[tuple[RouteDef, re.Pattern[str]]] = []
        for route in routes:
            self._validate_route(route)
            signature = (route.method, route.template)
            if signature in signatures:
                msg = f"duplicate route: {route.method.value} {route.template}"
                raise ValueError(msg)
            for existing_route, _ in compiled_routes:
                if route.method == existing_route.method and _templates_intersect(
                    route.template, existing_route.template
                ):
                    msg = f"intersecting route: {route.method.value} {route.template} and {existing_route.template}"
                    raise ValueError(msg)
            signatures.add(signature)
            compiled_routes.append((route, _compile_template(route.template)))

        object.__setattr__(self, "routes", routes)
        object.__setattr__(self, "_compiled_routes", tuple(compiled_routes))

    def match(self, method: HttpMethod | str, path: str) -> RouteMatch | None:
        """Return the route and parameters for an exact method/path match."""
        try:
            normalized_method = HttpMethod(method.upper()) if isinstance(method, str) else method
        except ValueError:
            return None

        for route, pattern in self._compiled_routes:
            if route.method != normalized_method:
                continue
            result = pattern.fullmatch(path)
            if result is not None:
                return RouteMatch(route=route, params=result.groupdict())
        return None

    def allowed_methods(self, path: str) -> tuple[HttpMethod, ...]:
        """Return the sorted methods whose route templates match *path*."""
        methods = {route.method for route, pattern in self._compiled_routes if pattern.fullmatch(path) is not None}
        return tuple(sorted(methods, key=lambda method: method.value))

    def validate_capabilities(self, capabilities: Iterable[str]) -> None:
        """Raise when this registry requires a capability the backend lacks."""
        available = frozenset(capabilities)
        missing = sorted({route.capability for route in self.routes} - available)
        if missing:
            msg = f"missing route capabilities: {', '.join(missing)}"
            raise ValueError(msg)

    @staticmethod
    def _validate_route(route: RouteDef) -> None:
        if not isinstance(route, RouteDef):
            raise TypeError("routes must contain RouteDef values")
        if not isinstance(route.method, HttpMethod):
            raise ValueError("route method must be an HttpMethod")
        if not isinstance(route.scope, RouteScope):
            raise ValueError("route scope must be a RouteScope")
        _validate_nonblank_normalized(route.operation, "operation")
        _validate_nonblank_normalized(route.capability, "capability")
        parameters = _template_parameters(route.template)
        if route.scope is RouteScope.SESSION and "session_id" not in parameters:
            raise ValueError("session route template must include {session_id}")


def _template_parameters(template: str) -> tuple[str, ...]:
    """Validate a normalized template and return its named parameters."""
    if not isinstance(template, str) or not template.startswith("/api/") or template.endswith("/"):
        raise ValueError("invalid route template")
    if "?" in template or "#" in template:
        raise ValueError("invalid route template")

    parameters: list[str] = []
    for segment in template.split("/")[1:]:
        if not segment:
            raise ValueError("invalid route template")
        if segment.startswith("{") and segment.endswith("}"):
            name = segment[1:-1]
            if name not in _PARAMETER_NAMES or name in parameters:
                raise ValueError("invalid route template parameter")
            parameters.append(name)
        elif "{" in segment or "}" in segment or _STATIC_SEGMENT_PATTERN.fullmatch(segment) is None:
            raise ValueError("invalid route template")
    return tuple(parameters)


def _validate_nonblank_normalized(value: object, field_name: str) -> None:
    """Require a stable, nonblank identifier for a route metadata field."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"route {field_name} must be nonblank and normalized")


def _templates_intersect(first: str, second: str) -> bool:
    """Return whether two validated templates can match the same path."""
    first_segments = first.split("/")[1:]
    second_segments = second.split("/")[1:]
    if len(first_segments) != len(second_segments):
        return False
    for first_segment, second_segment in zip(first_segments, second_segments, strict=True):
        first_parameter = first_segment.startswith("{")
        second_parameter = second_segment.startswith("{")
        if first_parameter and second_parameter:
            continue
        if first_parameter:
            if re.fullmatch(_PARAMETER_PATTERN, second_segment) is None:
                return False
            continue
        if second_parameter:
            if re.fullmatch(_PARAMETER_PATTERN, first_segment) is None:
                return False
            continue
        if first_segment != second_segment:
            return False
    return True


def _compile_template(template: str) -> re.Pattern[str]:
    """Compile a validated template into a full-path matcher."""
    parts: list[str] = []
    for segment in template.split("/")[1:]:
        if segment.startswith("{"):
            parts.append(f"(?P<{segment[1:-1]}>{_PARAMETER_PATTERN})")
        else:
            parts.append(re.escape(segment))
    return re.compile("^/" + "/".join(parts) + "$")
