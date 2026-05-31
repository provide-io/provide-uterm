# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for configurable security headers in the Cloudflare Worker."""

from __future__ import annotations

from types import SimpleNamespace

from provide.uterm.cloudflare.cf_types import Response
from provide.uterm.cloudflare.config import CloudflareConfig
from provide.uterm.cloudflare.entry import Default
from provide.uterm.cloudflare.entry.security import (
    _apply_security_headers,
    _resolve_security_headers,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_VALID_JWT_ENV: dict[str, object] = {
    "AUTH_MODE": "jwt",
    "JWT_ALGORITHMS": "HS256",
    "JWT_PUBLIC_KEY_PEM": "test-secret-key-32-bytes-minimum!",
    "WORKER_BEARER_TOKEN": "test-worker-token-padded-to-32xyz",
}


def _make_config(**kwargs: object) -> CloudflareConfig:
    """Build a CloudflareConfig (jwt mode) with optional overrides."""
    env: dict[str, object] = dict(_VALID_JWT_ENV)
    env.update(kwargs)
    return CloudflareConfig.from_env(SimpleNamespace(**env))


def _make_response(status: int = 200) -> Response:
    """Create a stub Response with a plain-dict headers attribute."""
    return Response(body="ok", status=status, headers={})


def _make_default(env_attrs: dict | None = None) -> Default:
    attrs: dict = dict(_VALID_JWT_ENV)
    if env_attrs:
        attrs.update(env_attrs)
    return Default(SimpleNamespace(**attrs))


def _req(path: str) -> SimpleNamespace:
    return SimpleNamespace(url=f"https://x{path}")


# ---------------------------------------------------------------------------
# Unit tests: _resolve_security_headers
# ---------------------------------------------------------------------------


def test_resolve_security_headers_strict() -> None:
    """Strict mode returns all 6 default headers."""
    config = _make_config(SECURITY_MODE="strict")
    headers = dict(_resolve_security_headers(config))
    assert "Content-Security-Policy" in headers
    assert "Strict-Transport-Security" in headers
    assert "X-Frame-Options" in headers
    assert "X-Content-Type-Options" in headers
    assert "Referrer-Policy" in headers
    assert "Permissions-Policy" in headers
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "max-age=63072000" in headers["Strict-Transport-Security"]
    assert "default-src 'self'" in headers["Content-Security-Policy"]


def test_resolve_security_headers_dev() -> None:
    """Dev mode returns only X-Content-Type-Options: nosniff."""
    config = _make_config(SECURITY_MODE="dev")
    headers = dict(_resolve_security_headers(config))
    assert list(headers.keys()) == ["X-Content-Type-Options"]
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_resolve_security_headers_unknown_mode_defaults_to_strict() -> None:
    """Unknown SECURITY_MODE is normalised to strict."""
    config = _make_config(SECURITY_MODE="bogus")
    headers = dict(_resolve_security_headers(config))
    assert "Content-Security-Policy" in headers


# ---------------------------------------------------------------------------
# Unit tests: _apply_security_headers
# ---------------------------------------------------------------------------


def test_apply_security_headers_strict_mode_all_headers() -> None:
    """_apply_security_headers in strict mode sets all 6 headers on a dict."""
    config = _make_config(SECURITY_MODE="strict")
    resp = _make_response()
    _apply_security_headers(resp, config)
    assert resp.headers is not None
    assert "Content-Security-Policy" in resp.headers
    assert "Strict-Transport-Security" in resp.headers
    assert "X-Frame-Options" in resp.headers
    assert "X-Content-Type-Options" in resp.headers
    assert "Referrer-Policy" in resp.headers
    assert "Permissions-Policy" in resp.headers


def test_apply_security_headers_dev_mode_only_nosniff() -> None:
    """_apply_security_headers in dev mode sets only X-Content-Type-Options."""
    config = _make_config(SECURITY_MODE="dev")
    resp = _make_response()
    _apply_security_headers(resp, config)
    assert resp.headers is not None
    assert list(resp.headers.keys()) == ["X-Content-Type-Options"]


def test_apply_security_headers_override_csp() -> None:
    """Custom SECURITY_CSP replaces the default CSP header."""
    custom_csp = "default-src 'none'"
    config = _make_config(SECURITY_MODE="strict", SECURITY_CSP=custom_csp)
    resp = _make_response()
    _apply_security_headers(resp, config)
    assert resp.headers is not None
    assert resp.headers["Content-Security-Policy"] == custom_csp


def test_apply_security_headers_empty_string_suppresses() -> None:
    """Empty string override suppresses the header entirely."""
    config = _make_config(SECURITY_MODE="strict", SECURITY_X_FRAME_OPTIONS="")
    resp = _make_response()
    _apply_security_headers(resp, config)
    assert resp.headers is not None
    assert "X-Frame-Options" not in resp.headers


def test_apply_security_headers_uses_set_method_on_headers_object() -> None:
    """_apply_security_headers calls .set() on a Headers-like object (CF runtime)."""
    config = _make_config(SECURITY_MODE="strict")
    resp = _make_response()
    # Replace headers with an object that has a .set() method
    recorded: dict[str, str] = {}

    class _FakeHeaders:
        def set(self, name: str, value: str) -> None:
            recorded[name] = value

    resp.headers = _FakeHeaders()  # type: ignore[assignment]
    _apply_security_headers(resp, config)
    assert "Content-Security-Policy" in recorded
    assert "X-Content-Type-Options" in recorded


def test_apply_security_headers_no_headers_attr_is_noop() -> None:
    """If response.headers is None, _apply_security_headers is a no-op."""
    config = _make_config()
    resp = Response(body="ok", status=200, headers=None)
    # Should not raise
    result = _apply_security_headers(resp, config)
    assert result is resp


# ---------------------------------------------------------------------------
# Config: from_env reads security env vars
# ---------------------------------------------------------------------------


def test_config_from_env_security_mode_strict() -> None:
    config = _make_config(SECURITY_MODE="strict")
    assert config.security_mode == "strict"


def test_config_from_env_security_mode_dev() -> None:
    config = _make_config(SECURITY_MODE="dev")
    assert config.security_mode == "dev"


def test_config_from_env_security_mode_invalid_defaults_to_strict() -> None:
    config = _make_config(SECURITY_MODE="unknown")
    assert config.security_mode == "strict"


def test_config_from_env_security_mode_default_is_strict() -> None:
    config = _make_config()
    assert config.security_mode == "strict"


def test_config_from_env_security_csp_override() -> None:
    config = _make_config(SECURITY_CSP="default-src 'none'")
    assert config.security_csp == "default-src 'none'"


def test_config_from_env_security_csp_not_set_is_none() -> None:
    config = _make_config()
    assert config.security_csp is None


def test_config_from_env_security_csp_empty_string() -> None:
    """Empty SECURITY_CSP env var is preserved as '' (suppress signal)."""
    config = _make_config(SECURITY_CSP="")
    assert config.security_csp == ""


def test_config_from_env_all_security_overrides() -> None:
    config = _make_config(
        SECURITY_MODE="strict",
        SECURITY_CSP="default-src 'none'",
        SECURITY_HSTS="max-age=0",
        SECURITY_X_FRAME_OPTIONS="SAMEORIGIN",
        SECURITY_X_CONTENT_TYPE_OPTIONS="nosniff",
        SECURITY_REFERRER_POLICY="no-referrer",
        SECURITY_PERMISSIONS_POLICY="",
    )
    assert config.security_mode == "strict"
    assert config.security_csp == "default-src 'none'"
    assert config.security_hsts == "max-age=0"
    assert config.security_x_frame_options == "SAMEORIGIN"
    assert config.security_x_content_type_options == "nosniff"
    assert config.security_referrer_policy == "no-referrer"
    assert config.security_permissions_policy == ""


# ---------------------------------------------------------------------------
# Integration: Default.fetch() applies security headers on HTTP responses
# ---------------------------------------------------------------------------


async def test_default_fetch_health_has_security_headers() -> None:
    """/api/health response includes security headers (strict mode default)."""
    d = _make_default()
    resp = await d.fetch(_req("/api/health"))
    assert resp.status == 200
    assert resp.headers is not None
    assert "X-Content-Type-Options" in resp.headers


async def test_default_fetch_security_headers_strict_on_200() -> None:
    """All 6 strict-mode headers present on a 200 /api/health response."""
    d = _make_default()
    resp = await d.fetch(_req("/api/health"))
    assert resp.headers is not None
    for header in (
        "Content-Security-Policy",
        "Strict-Transport-Security",
        "X-Frame-Options",
        "X-Content-Type-Options",
        "Referrer-Policy",
        "Permissions-Policy",
    ):
        assert header in resp.headers, f"Missing header: {header}"


async def test_default_fetch_dev_security_mode_only_nosniff() -> None:
    """SECURITY_MODE=dev → only nosniff header on HTTP responses."""
    d = _make_default({"SECURITY_MODE": "dev"})
    resp = await d.fetch(_req("/api/health"))
    assert resp.headers is not None
    assert "X-Content-Type-Options" in resp.headers
    assert "X-Frame-Options" not in resp.headers
    assert "Content-Security-Policy" not in resp.headers


async def test_default_fetch_custom_csp_applied() -> None:
    """SECURITY_CSP override is reflected in the response."""
    custom = "default-src 'none'"
    d = _make_default({"SECURITY_CSP": custom})
    resp = await d.fetch(_req("/api/health"))
    assert resp.headers is not None
    assert resp.headers.get("Content-Security-Policy") == custom


async def test_default_fetch_csp_suppressed_by_empty_string() -> None:
    """SECURITY_CSP='' suppresses CSP header in response."""
    d = _make_default({"SECURITY_CSP": ""})
    resp = await d.fetch(_req("/api/health"))
    assert resp.headers is not None
    assert "Content-Security-Policy" not in resp.headers


async def test_default_fetch_101_response_no_security_headers() -> None:
    """WebSocket 101 responses do not have security headers applied."""
    # Make the DO return a 101 response stub directly — no need for a real SessionRuntime.
    ws_resp = Response(body=None, status=101, headers={})

    async def _fake_fetch(req: object) -> Response:
        return ws_resp

    stub = SimpleNamespace(fetch=_fake_fetch)
    ns = SimpleNamespace(idFromName=lambda wid: "sid", get=lambda sid: stub)

    d = _make_default({"SESSION_RUNTIME": ns})
    resp = await d.fetch(_req("/ws/browser/test-session/term"))
    assert resp.status == 101
    # Security headers must NOT be injected on WebSocket upgrade responses
    assert "X-Frame-Options" not in (resp.headers or {})
    assert "Content-Security-Policy" not in (resp.headers or {})
