#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Configurable security response headers middleware."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from provide.terminal.server.models import SecurityConfig

# Strict mode defaults
_STRICT_DEFAULTS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' cdn.jsdelivr.net fonts.googleapis.com; "
        "font-src fonts.gstatic.com; "
        "connect-src 'self' ws: wss:; "
        "img-src 'self' data:"
    ),
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

# Dev mode defaults (only nosniff is always set)
_DEV_DEFAULTS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
}

# Map config field names to HTTP header names
_FIELD_TO_HEADER: dict[str, str] = {
    "csp": "Content-Security-Policy",
    "hsts": "Strict-Transport-Security",
    "x_frame_options": "X-Frame-Options",
    "x_content_type_options": "X-Content-Type-Options",
    "referrer_policy": "Referrer-Policy",
    "permissions_policy": "Permissions-Policy",
}


def _resolve_headers(config: SecurityConfig) -> list[tuple[str, str]]:
    """Build the final header list from config, merging overrides with mode defaults."""
    defaults = _STRICT_DEFAULTS if config.mode == "strict" else _DEV_DEFAULTS
    result: list[tuple[str, str]] = []
    for field, header in _FIELD_TO_HEADER.items():
        override = getattr(config, field)
        if override is not None:
            if override:  # non-empty string → use it
                result.append((header, override))
            # empty string → suppress the header entirely
        elif header in defaults:
            result.append((header, defaults[header]))
    return result


class SecurityHeadersMiddleware:
    """Raw ASGI middleware that injects security headers on HTTP responses."""

    def __init__(self, app: Any, config: SecurityConfig, auth_mode: str | None = None) -> None:
        self.app = app
        self._headers = _resolve_headers(config)
        if auth_mode is not None and auth_mode in {"dev", "none"}:
            self._headers = [*self._headers, ("X-Auth-Mode", auth_mode)]

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                for name, value in self._headers:
                    headers.append((name.encode(), value.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
