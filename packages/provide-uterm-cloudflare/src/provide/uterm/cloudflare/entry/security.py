"""Security-header resolution and application for HTTP responses."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from provide.uterm.cloudflare.entry.fallback_stubs import CloudflareConfig, Response

_STRICT_DEFAULTS: tuple[tuple[str, str], ...] = (
    (
        "Content-Security-Policy",
        (
            "default-src 'self'; "
            "script-src 'self' cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' cdn.jsdelivr.net fonts.googleapis.com; "
            "font-src fonts.gstatic.com; "
            "connect-src 'self' ws: wss:; "
            "img-src 'self' data:"
        ),
    ),
    ("Strict-Transport-Security", "max-age=63072000; includeSubDomains"),
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
)

_DEV_DEFAULTS: tuple[tuple[str, str], ...] = (("X-Content-Type-Options", "nosniff"),)

_OVERRIDE_FIELDS: tuple[tuple[str, str], ...] = (
    ("Content-Security-Policy", "security_csp"),
    ("Strict-Transport-Security", "security_hsts"),
    ("X-Frame-Options", "security_x_frame_options"),
    ("X-Content-Type-Options", "security_x_content_type_options"),
    ("Referrer-Policy", "security_referrer_policy"),
    ("Permissions-Policy", "security_permissions_policy"),
)


def _resolve_security_headers(config: CloudflareConfig) -> list[tuple[str, str]]:
    """Compute the list of (header-name, value) pairs to apply based on config.

    Resolution order:
    1. Start with mode defaults (strict or dev).
    2. Per-field override: if the config field is not None, replace (non-empty)
       or suppress (empty string) the header.
    """
    mode_defaults = _STRICT_DEFAULTS if config.security_mode != "dev" else _DEV_DEFAULTS
    result: dict[str, str] = dict(mode_defaults)
    for header_name, field_name in _OVERRIDE_FIELDS:
        override = getattr(config, field_name, None)
        if override is None:
            continue
        if override == "":
            result.pop(header_name, None)
        else:
            result[header_name] = override
    return list(result.items())


def _apply_security_headers(response: Response, config: CloudflareConfig) -> Response:
    """Add security headers to an HTTP response based on config.

    Works with both the real CF Runtime Headers object (has .set()) and the
    test stub (plain dict).  WebSocket 101 responses should not be passed here.
    """
    headers = _resolve_security_headers(config)
    resp_headers = getattr(response, "headers", None)
    if resp_headers is None:
        return response
    set_fn = getattr(resp_headers, "set", None)
    if callable(set_fn):
        for name, value in headers:
            set_fn(name, value)
    else:
        # Plain dict (test stub)
        for name, value in headers:
            resp_headers[name] = value
    return response


__all__ = [
    "_DEV_DEFAULTS",
    "_OVERRIDE_FIELDS",
    "_STRICT_DEFAULTS",
    "_apply_security_headers",
    "_resolve_security_headers",
]
