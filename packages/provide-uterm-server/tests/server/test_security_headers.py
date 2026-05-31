#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for configurable security response headers middleware."""

from __future__ import annotations

from fastapi import FastAPI
from starlette.testclient import TestClient

from provide.uterm.server.models import SecurityConfig
from provide.uterm.server.security import (
    _DEV_DEFAULTS,
    _FIELD_TO_HEADER,
    _STRICT_DEFAULTS,
    SecurityHeadersMiddleware,
    _resolve_headers,
)


def _make_app(config: SecurityConfig) -> FastAPI:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware, config=config)

    @app.get("/test")
    def test_endpoint() -> dict[str, bool]:
        return {"ok": True}

    return app


# ---------------------------------------------------------------------------
# Unit tests for _resolve_headers
# ---------------------------------------------------------------------------


class TestResolveHeadersStrict:
    def test_strict_defaults(self) -> None:
        config = SecurityConfig(mode="strict")
        headers = _resolve_headers(config)
        header_dict = dict(headers)
        for header, value in _STRICT_DEFAULTS.items():
            assert header_dict[header] == value

    def test_all_six_headers_present(self) -> None:
        config = SecurityConfig(mode="strict")
        headers = _resolve_headers(config)
        assert len(headers) == len(_STRICT_DEFAULTS)


class TestResolveHeadersDev:
    def test_dev_defaults(self) -> None:
        config = SecurityConfig(mode="dev")
        headers = _resolve_headers(config)
        header_dict = dict(headers)
        assert header_dict == _DEV_DEFAULTS

    def test_only_nosniff(self) -> None:
        config = SecurityConfig(mode="dev")
        headers = _resolve_headers(config)
        assert len(headers) == 1
        assert headers[0] == ("X-Content-Type-Options", "nosniff")


class TestResolveHeadersOverrides:
    def test_override_csp(self) -> None:
        config = SecurityConfig(mode="strict", csp="default-src 'none'")
        headers = _resolve_headers(config)
        header_dict = dict(headers)
        assert header_dict["Content-Security-Policy"] == "default-src 'none'"

    def test_empty_string_suppresses(self) -> None:
        config = SecurityConfig(mode="strict", hsts="")
        headers = _resolve_headers(config)
        header_names = [h[0] for h in headers]
        assert "Strict-Transport-Security" not in header_names

    def test_override_in_dev_mode(self) -> None:
        config = SecurityConfig(mode="dev", csp="default-src 'self'")
        headers = _resolve_headers(config)
        header_dict = dict(headers)
        assert header_dict["Content-Security-Policy"] == "default-src 'self'"
        assert "X-Content-Type-Options" in header_dict

    def test_empty_suppresses_dev_default(self) -> None:
        config = SecurityConfig(mode="dev", x_content_type_options="")
        headers = _resolve_headers(config)
        assert len(headers) == 0

    def test_all_fields_overridden(self) -> None:
        config = SecurityConfig(
            mode="strict",
            csp="custom-csp",
            hsts="custom-hsts",
            x_frame_options="SAMEORIGIN",
            x_content_type_options="nosniff",
            referrer_policy="no-referrer",
            permissions_policy="camera=(self)",
        )
        headers = _resolve_headers(config)
        header_dict = dict(headers)
        assert header_dict["Content-Security-Policy"] == "custom-csp"
        assert header_dict["Strict-Transport-Security"] == "custom-hsts"
        assert header_dict["X-Frame-Options"] == "SAMEORIGIN"
        assert header_dict["X-Content-Type-Options"] == "nosniff"
        assert header_dict["Referrer-Policy"] == "no-referrer"
        assert header_dict["Permissions-Policy"] == "camera=(self)"

    def test_all_suppressed(self) -> None:
        config = SecurityConfig(
            mode="strict",
            csp="",
            hsts="",
            x_frame_options="",
            x_content_type_options="",
            referrer_policy="",
            permissions_policy="",
        )
        headers = _resolve_headers(config)
        assert headers == []


# ---------------------------------------------------------------------------
# Integration tests with TestClient
# ---------------------------------------------------------------------------


class TestStrictModeIntegration:
    def test_strict_mode_sets_all_headers(self) -> None:
        app = _make_app(SecurityConfig(mode="strict"))
        client = TestClient(app)
        resp = client.get("/test")
        assert resp.status_code == 200
        for header, value in _STRICT_DEFAULTS.items():
            assert resp.headers[header] == value


class TestDevModeIntegration:
    def test_dev_mode_only_nosniff(self) -> None:
        app = _make_app(SecurityConfig(mode="dev"))
        client = TestClient(app)
        resp = client.get("/test")
        assert resp.status_code == 200
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        # Strict-only headers must be absent
        for header in _STRICT_DEFAULTS:
            if header != "X-Content-Type-Options":
                assert header not in resp.headers


class TestOverrideIntegration:
    def test_override_csp(self) -> None:
        app = _make_app(SecurityConfig(mode="strict", csp="default-src 'none'"))
        client = TestClient(app)
        resp = client.get("/test")
        assert resp.headers["Content-Security-Policy"] == "default-src 'none'"

    def test_empty_string_suppresses_header(self) -> None:
        app = _make_app(SecurityConfig(mode="strict", hsts=""))
        client = TestClient(app)
        resp = client.get("/test")
        assert "Strict-Transport-Security" not in resp.headers
        # Other strict defaults still present
        assert resp.headers["X-Content-Type-Options"] == "nosniff"


class TestFieldToHeaderMapping:
    def test_all_fields_mapped(self) -> None:
        """Ensure every header-related config field has a mapping.

        Behavioral flags (``mode``, ``dev_mode_acknowledged``,
        ``block_private_connector_targets``) are excluded because they control
        connector egress or mode selection, not HTTP response headers.
        """
        _NON_HEADER_FIELDS = {"mode", "dev_mode_acknowledged", "block_private_connector_targets"}
        config_fields = {f for f in SecurityConfig.model_fields if f not in _NON_HEADER_FIELDS}
        assert config_fields == set(_FIELD_TO_HEADER.keys())
