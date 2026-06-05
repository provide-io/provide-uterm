#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Self-contained mutation-killing suite for server/routes/* helper functions.

mutmut SKIPS every decorated function (the ``@router.get``/``@router.post`` FastAPI
handlers — see _skip_node_and_children in mutmut/file_mutation.py), so the mutable
surface of routes/ is the UNDECORATED code: the shared ``_helpers.py`` accessors, the
per-module ``_registry``/``_authz``/``_principal`` accessors and ``create_*_router``
bodies, and the NESTED undecorated helpers defined inside ``create_*_router`` (e.g.
``_posture_caller_is_privileged``). None of it is async-streaming, so this suite needs
no SSE/timer mocking — it builds each router with mocked app state, pulls the handler
endpoints off ``router.routes``, and calls them directly with a mocked Request (no
TestClient / full-app lifespan, which is fragile in the mutants tree).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import APIRouter, HTTPException


def _request(
    *,
    app_state: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    url_scheme: str = "http",
    client_host: str | None = "1.2.3.4",
) -> MagicMock:
    req = MagicMock(name="request")
    req.app.state = SimpleNamespace(**(app_state or {}))
    req.state = SimpleNamespace(**(state or {}))
    req.headers = headers or {}
    req.url = SimpleNamespace(scheme=url_scheme)
    req.client = SimpleNamespace(host=client_host) if client_host is not None else None
    return req


def _endpoint(router: APIRouter, path: str, method: str = "GET") -> Any:
    for route in router.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise KeyError(f"{method} {path} not found in router")


def _paths(router: APIRouter) -> set[str]:
    return {getattr(r, "path", None) for r in router.routes}


_UNSET = object()  # sentinel so callers can pass principal=None explicitly


# ===========================================================================
# _helpers.py — shared route helpers
# ===========================================================================


class TestSetSpanAttrs:
    def test_sets_every_mapped_attribute(self) -> None:
        from provide.uterm.server.routes._helpers import set_span_attrs

        span = MagicMock()
        set_span_attrs(
            span,
            session_id="s1",
            worker_id="w1",
            operation="op",
            principal="alice",
            http_method="GET",
            http_path="/x",
        )
        span.set_attribute.assert_any_call("uterm.session_id", "s1")
        span.set_attribute.assert_any_call("uterm.worker_id", "w1")
        span.set_attribute.assert_any_call("uterm.operation", "op")
        span.set_attribute.assert_any_call("uterm.principal", "alice")
        span.set_attribute.assert_any_call("http.method", "GET")
        span.set_attribute.assert_any_call("http.target", "/x")
        assert span.set_attribute.call_count == 6

    def test_none_values_skipped(self) -> None:
        from provide.uterm.server.routes._helpers import set_span_attrs

        span = MagicMock()
        set_span_attrs(span, session_id="s1", worker_id=None)
        span.set_attribute.assert_called_once_with("uterm.session_id", "s1")

    def test_no_set_attribute_method_is_noop(self) -> None:
        from provide.uterm.server.routes._helpers import set_span_attrs

        span = SimpleNamespace()  # no set_attribute attr at all
        set_span_attrs(span, session_id="s1")  # must not raise

    def test_non_callable_set_attribute_is_noop(self) -> None:
        from provide.uterm.server.routes._helpers import set_span_attrs

        span = SimpleNamespace(set_attribute="not-callable")
        set_span_attrs(span, session_id="s1")  # not callable → early return, no crash


class TestHelperAccessors:
    def test_registry_returns_app_state(self) -> None:
        from provide.uterm.server.routes._helpers import registry

        reg = object()
        assert registry(_request(app_state={"uterm_registry": reg})) is reg

    def test_authz_returns_app_state(self) -> None:
        from provide.uterm.server.routes._helpers import authz

        az = object()
        assert authz(_request(app_state={"uterm_authz": az})) is az

    def test_principal_present_returned(self) -> None:
        from provide.uterm.server.routes._helpers import principal

        p = object()
        assert principal(_request(state={"uterm_principal": p})) is p

    def test_principal_missing_raises_500(self) -> None:
        from provide.uterm.server.routes._helpers import principal

        with pytest.raises(HTTPException) as exc:
            principal(_request(state={"uterm_principal": None}))
        assert exc.value.status_code == 500
        assert exc.value.detail == "principal was not resolved"

    def test_source_ip_from_client_host(self) -> None:
        from provide.uterm.server.routes._helpers import source_ip

        assert source_ip(_request(client_host="9.9.9.9")) == "9.9.9.9"

    def test_source_ip_no_client_is_unknown(self) -> None:
        from provide.uterm.server.routes._helpers import source_ip

        assert source_ip(_request(client_host=None)) == "unknown"

    def test_source_ip_client_without_host_is_unknown(self) -> None:
        from provide.uterm.server.routes._helpers import source_ip

        req = _request()
        req.client = SimpleNamespace()  # client present but no .host
        assert source_ip(req) == "unknown"

    async def test_session_definition_found(self) -> None:
        from provide.uterm.server.routes._helpers import session_definition

        sess = object()
        reg = MagicMock()
        reg.get_definition = AsyncMock(return_value=sess)
        result = await session_definition(_request(app_state={"uterm_registry": reg}), "s1")
        assert result is sess
        reg.get_definition.assert_awaited_once_with("s1")

    async def test_session_definition_missing_raises_404(self) -> None:
        from provide.uterm.server.routes._helpers import session_definition

        reg = MagicMock()
        reg.get_definition = AsyncMock(return_value=None)
        with pytest.raises(HTTPException) as exc:
            await session_definition(_request(app_state={"uterm_registry": reg}), "ghost")
        assert exc.value.status_code == 404
        assert exc.value.detail == "unknown session: ghost"

    def test_sid_not_found_builds_404(self) -> None:
        from provide.uterm.server.routes._helpers import sid_not_found

        exc = sid_not_found("ghost")
        assert isinstance(exc, HTTPException)
        assert exc.status_code == 404
        assert exc.detail == "unknown session: ghost"


# ===========================================================================
# health.py — create_health_router + nested _posture_caller_is_privileged
# ===========================================================================


class TestHealthRouter:
    def _router(self, *, with_auth: bool = False) -> APIRouter:
        from provide.uterm.server.routes.health import create_health_router

        dep = AsyncMock() if with_auth else None
        return create_health_router(require_authenticated=dep)

    def test_security_posture_omitted_without_dependency(self) -> None:
        assert "/api/security-posture" not in _paths(self._router(with_auth=False))

    def test_security_posture_present_with_dependency(self) -> None:
        assert "/api/security-posture" in _paths(self._router(with_auth=True))

    async def test_health_unavailable_when_no_registry(self) -> None:
        health = _endpoint(self._router(), "/api/health")
        resp = MagicMock()
        out = await health(_request(app_state={"uterm_registry": None}), resp)
        assert resp.status_code == 503
        assert out == {"status": "unavailable", "ok": False, "ready": False, "service": "uterm-server"}

    async def test_health_starting_when_not_ready(self) -> None:
        health = _endpoint(self._router(), "/api/health")
        resp = MagicMock()
        out = await health(_request(app_state={"uterm_registry": MagicMock(), "uterm_ready": False}), resp)
        assert resp.status_code == 503
        assert out["status"] == "starting"

    async def test_health_ok_reports_sessions_and_backend(self) -> None:
        health = _endpoint(self._router(), "/api/health")
        reg = SimpleNamespace(_sessions={"a": 1, "b": 2})
        cfg = SimpleNamespace(control_plane=SimpleNamespace(backend="postgres"))
        resp = MagicMock()
        out = await health(
            _request(
                app_state={
                    "uterm_registry": reg,
                    "uterm_ready": True,
                    "uterm_startup_time": 0.0,
                    "uterm_config": cfg,
                }
            ),
            resp,
        )
        assert out["status"] == "ok"
        assert out["ok"] is True
        assert out["active_sessions"] == 2
        assert out["control_plane_backend"] == "postgres"
        assert out["service"] == "uterm-server"

    async def test_healthz_always_ok(self) -> None:
        healthz = _endpoint(self._router(), "/healthz")
        assert await healthz() == {"status": "ok"}

    async def test_readyz_ready_and_not_ready(self) -> None:
        readyz = _endpoint(self._router(), "/readyz")
        resp = MagicMock()
        assert await readyz(_request(app_state={"uterm_ready": True}), resp) == {"status": "ready"}
        resp2 = MagicMock()
        out = await readyz(_request(app_state={"uterm_ready": False}), resp2)
        assert resp2.status_code == 503
        assert out == {"status": "not_ready"}

    async def test_security_posture_full_for_privileged(self) -> None:
        from unittest.mock import patch

        posture = {"environment": "prod", "secure": True, "dev_opt_outs": ["x"], "warnings": ["w"]}
        sp = _endpoint(self._router(with_auth=True), "/api/security-posture")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=True)
        authz.has_role = AsyncMock(return_value=False)
        req = _request(app_state={"uterm_config": object(), "uterm_authz": authz}, state={"uterm_principal": object()})
        with patch("provide.uterm.server.routes.health.compute_security_posture", return_value=posture):
            out = await sp(req)
        assert out == posture  # privileged → full report

    async def test_security_posture_coarse_for_non_privileged(self) -> None:
        from unittest.mock import patch

        posture = {"environment": "prod", "secure": True, "dev_opt_outs": ["x"], "warnings": ["w"]}
        sp = _endpoint(self._router(with_auth=True), "/api/security-posture")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=False)
        authz.has_role = AsyncMock(return_value=False)
        req = _request(app_state={"uterm_config": object(), "uterm_authz": authz}, state={"uterm_principal": object()})
        with patch("provide.uterm.server.routes.health.compute_security_posture", return_value=posture):
            out = await sp(req)
        assert out == {"environment": "prod", "secure": True}  # coarse summary only

    async def test_security_posture_coarse_when_principal_or_authz_missing(self) -> None:
        from unittest.mock import patch

        posture = {"environment": "dev", "secure": False, "dev_opt_outs": [], "warnings": []}
        sp = _endpoint(self._router(with_auth=True), "/api/security-posture")
        # principal None → _posture_caller_is_privileged returns False → coarse
        req = _request(app_state={"uterm_config": object(), "uterm_authz": None}, state={"uterm_principal": None})
        with patch("provide.uterm.server.routes.health.compute_security_posture", return_value=posture):
            out = await sp(req)
        assert out == {"environment": "dev", "secure": False}

    async def test_security_posture_operator_role_is_privileged(self) -> None:
        from unittest.mock import patch

        posture = {"environment": "prod", "secure": True, "dev_opt_outs": ["x"], "warnings": []}
        sp = _endpoint(self._router(with_auth=True), "/api/security-posture")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=False)
        authz.has_role = AsyncMock(return_value=True)  # operator
        req = _request(app_state={"uterm_config": object(), "uterm_authz": authz}, state={"uterm_principal": object()})
        with patch("provide.uterm.server.routes.health.compute_security_posture", return_value=posture):
            out = await sp(req)
        assert out == posture
        authz.has_role.assert_awaited_once_with(req.state.uterm_principal, "operator")


# ===========================================================================
# pages.py — module helpers + create_page_router (nested undecorated bodies)
# ===========================================================================


def _pages_auth_cfg(mode: str = "jwt") -> SimpleNamespace:
    return SimpleNamespace(
        mode=mode,
        principal_cookie="uterm_principal",
        surface_cookie="uterm_surface",
        token_cookie="uterm_token",
    )


def _pages_tunnel_cfg(*, cookie_secure: bool = True, cookie_samesite: str = "lax") -> SimpleNamespace:
    return SimpleNamespace(cookie_secure=cookie_secure, cookie_samesite=cookie_samesite)


def _pages_ui_cfg() -> SimpleNamespace:
    return SimpleNamespace(
        app_path="/app",
        assets_path="/assets",
        xterm_cdn="xc",
        fitaddon_cdn="fc",
        fonts_cdn="fonts-cdn-url",
        xterm_cdn_integrity="xi",
        fitaddon_cdn_integrity="fi",
    )


def _pages_cfg(*, mode: str = "jwt", cookie_secure: bool = True, cookie_samesite: str = "lax") -> SimpleNamespace:
    return SimpleNamespace(
        auth=_pages_auth_cfg(mode),
        tunnel=_pages_tunnel_cfg(cookie_secure=cookie_secure, cookie_samesite=cookie_samesite),
        ui=_pages_ui_cfg(),
        server=SimpleNamespace(title="Term"),
    )


class TestPagesRoutes:
    # ---- _is_secure_request -------------------------------------------------

    def test_is_secure_true_when_forwarded_proto_has_https(self) -> None:
        from provide.uterm.server.routes.pages import _is_secure_request

        req = _request(headers={"x-forwarded-proto": "https,http"}, url_scheme="http")
        assert _is_secure_request(req) is True

    def test_is_secure_true_when_scheme_https_and_no_forwarded(self) -> None:
        from provide.uterm.server.routes.pages import _is_secure_request

        req = _request(headers={}, url_scheme="https")
        assert _is_secure_request(req) is True

    def test_is_secure_false_when_neither_https(self) -> None:
        from provide.uterm.server.routes.pages import _is_secure_request

        req = _request(headers={"x-forwarded-proto": "http"}, url_scheme="http")
        assert _is_secure_request(req) is False

    def test_is_secure_forwarded_proto_is_lowercased(self) -> None:
        from provide.uterm.server.routes.pages import _is_secure_request

        req = _request(headers={"x-forwarded-proto": "HTTPS"}, url_scheme="http")
        assert _is_secure_request(req) is True

    def test_is_secure_scheme_compared_against_exact_https(self) -> None:
        from provide.uterm.server.routes.pages import _is_secure_request

        req = _request(headers={}, url_scheme="httpsx")
        assert _is_secure_request(req) is False

    # ---- _set_auth_cookie ---------------------------------------------------

    def test_set_auth_cookie_default_kwargs(self) -> None:
        from provide.uterm.server.routes.pages import _set_auth_cookie

        resp = MagicMock()
        _set_auth_cookie(resp, "k", "v", secure=True)
        resp.set_cookie.assert_called_once_with(key="k", value="v", secure=True, httponly=True, samesite="lax")

    def test_set_auth_cookie_secure_false_passes_through(self) -> None:
        from provide.uterm.server.routes.pages import _set_auth_cookie

        resp = MagicMock()
        _set_auth_cookie(resp, "k", "v", secure=False)
        resp.set_cookie.assert_called_once_with(key="k", value="v", secure=False, httponly=True, samesite="lax")

    def test_set_auth_cookie_explicit_samesite(self) -> None:
        from provide.uterm.server.routes.pages import _set_auth_cookie

        resp = MagicMock()
        _set_auth_cookie(resp, "k", "v", secure=True, samesite="strict")
        resp.set_cookie.assert_called_once_with(key="k", value="v", secure=True, httponly=True, samesite="strict")

    # ---- _set_page_cookies --------------------------------------------------

    def test_set_page_cookies_jwt_named_principal_sets_token(self) -> None:
        from provide.uterm.server.routes.pages import _set_page_cookies

        resp = MagicMock()
        cfg = _pages_cfg(mode="jwt")
        req = _request(headers={"authorization": "Bearer tok-abc"}, state={})
        with patch("provide.uterm.server.routes.pages.extract_bearer_token", return_value="tok-abc") as eb:
            _set_page_cookies(resp, req, cfg, "alice", "operator", secure=True)
        eb.assert_called_once_with(req.headers)
        kwargs = [c.kwargs for c in resp.set_cookie.call_args_list]
        assert {
            "key": "uterm_principal",
            "value": "alice",
            "secure": True,
            "httponly": True,
            "samesite": "lax",
        } in kwargs
        assert {
            "key": "uterm_surface",
            "value": "operator",
            "secure": True,
            "httponly": True,
            "samesite": "lax",
        } in kwargs
        assert {"key": "uterm_token", "value": "tok-abc", "secure": True, "httponly": True, "samesite": "lax"} in kwargs

    def test_set_page_cookies_anonymous_skips_token(self) -> None:
        from provide.uterm.server.routes.pages import _set_page_cookies

        resp = MagicMock()
        cfg = _pages_cfg(mode="jwt")
        req = _request(headers={"authorization": "Bearer tok-abc"}, state={})
        with patch("provide.uterm.server.routes.pages.extract_bearer_token", return_value="tok-abc") as eb:
            _set_page_cookies(resp, req, cfg, "anonymous", "operator", secure=True)
        eb.assert_not_called()
        keys = {c.kwargs["key"] for c in resp.set_cookie.call_args_list}
        assert "uterm_token" not in keys
        assert keys == {"uterm_principal", "uterm_surface"}

    def test_set_page_cookies_non_jwt_mode_skips_token(self) -> None:
        from provide.uterm.server.routes.pages import _set_page_cookies

        resp = MagicMock()
        cfg = _pages_cfg(mode="header")
        req = _request(headers={"authorization": "Bearer tok-abc"}, state={})
        with patch("provide.uterm.server.routes.pages.extract_bearer_token", return_value="tok-abc") as eb:
            _set_page_cookies(resp, req, cfg, "alice", "operator", secure=False)
        eb.assert_not_called()
        keys = {c.kwargs["key"] for c in resp.set_cookie.call_args_list}
        assert "uterm_token" not in keys

    def test_set_page_cookies_jwt_no_bearer_skips_token(self) -> None:
        from provide.uterm.server.routes.pages import _set_page_cookies

        resp = MagicMock()
        cfg = _pages_cfg(mode="jwt")
        req = _request(headers={}, state={})
        with patch("provide.uterm.server.routes.pages.extract_bearer_token", return_value=None) as eb:
            _set_page_cookies(resp, req, cfg, "alice", "operator", secure=True)
        eb.assert_called_once_with(req.headers)
        keys = {c.kwargs["key"] for c in resp.set_cookie.call_args_list}
        assert "uterm_token" not in keys

    def test_set_page_cookies_tunnel_cookie_secure_inherits_secure(self) -> None:
        from provide.uterm.server.routes.pages import _set_page_cookies

        resp = MagicMock()
        cfg = _pages_cfg(mode="header", cookie_secure=True, cookie_samesite="strict")
        req = _request(headers={}, state={"uterm_share_token": "shtok"})
        _set_page_cookies(resp, req, cfg, "alice", "user", secure=True, session_id="sess1")
        tunnel = [c.kwargs for c in resp.set_cookie.call_args_list if c.kwargs["key"] == "uterm_tunnel_sess1"]
        assert tunnel == [
            {"key": "uterm_tunnel_sess1", "value": "shtok", "secure": True, "httponly": True, "samesite": "strict"}
        ]

    def test_set_page_cookies_tunnel_cookie_secure_false_forces_insecure(self) -> None:
        from provide.uterm.server.routes.pages import _set_page_cookies

        resp = MagicMock()
        cfg = _pages_cfg(mode="header", cookie_secure=False, cookie_samesite="lax")
        req = _request(headers={}, state={"uterm_share_token": "shtok"})
        _set_page_cookies(resp, req, cfg, "alice", "user", secure=True, session_id="sess1")
        tunnel = [c.kwargs for c in resp.set_cookie.call_args_list if c.kwargs["key"] == "uterm_tunnel_sess1"]
        assert tunnel == [
            {"key": "uterm_tunnel_sess1", "value": "shtok", "secure": False, "httponly": True, "samesite": "lax"}
        ]

    def test_set_page_cookies_no_share_token_no_tunnel_cookie(self) -> None:
        from provide.uterm.server.routes.pages import _set_page_cookies

        resp = MagicMock()
        cfg = _pages_cfg(mode="header")
        req = _request(headers={}, state={})
        _set_page_cookies(resp, req, cfg, "alice", "user", secure=True, session_id="sess1")
        keys = {c.kwargs["key"] for c in resp.set_cookie.call_args_list}
        assert not any(k.startswith("uterm_tunnel_") for k in keys)

    def test_set_page_cookies_share_token_but_no_session_id_no_tunnel_cookie(self) -> None:
        from provide.uterm.server.routes.pages import _set_page_cookies

        resp = MagicMock()
        cfg = _pages_cfg(mode="header")
        req = _request(headers={}, state={"uterm_share_token": "shtok"})
        _set_page_cookies(resp, req, cfg, "alice", "user", secure=True)
        keys = {c.kwargs["key"] for c in resp.set_cookie.call_args_list}
        assert not any(k.startswith("uterm_tunnel_") for k in keys)

    def test_set_page_cookies_tunnel_value_is_stringified(self) -> None:
        from provide.uterm.server.routes.pages import _set_page_cookies

        resp = MagicMock()
        cfg = _pages_cfg(mode="header", cookie_secure=True)
        req = _request(headers={}, state={"uterm_share_token": 12345})
        _set_page_cookies(resp, req, cfg, "alice", "user", secure=True, session_id="s2")
        tunnel = [c.kwargs for c in resp.set_cookie.call_args_list if c.kwargs["key"] == "uterm_tunnel_s2"]
        assert tunnel[0]["value"] == "12345"

    # ---- _share_role --------------------------------------------------------

    def test_share_role_returns_state_value(self) -> None:
        from provide.uterm.server.routes.pages import _share_role

        assert _share_role(_request(state={"uterm_share_role": "operator"})) == "operator"

    def test_share_role_none_when_absent(self) -> None:
        from provide.uterm.server.routes.pages import _share_role

        assert _share_role(_request(state={})) is None

    # ---- create_page_router: structure -------------------------------------

    def test_router_has_all_expected_paths(self) -> None:
        from provide.uterm.server.routes.pages import create_page_router

        assert _paths(create_page_router()) == {
            "/",
            "/session/{session_id}",
            "/operator/{session_id}",
            "/replay/{session_id}",
            "/inspect/{session_id}",
            "/connect",
        }

    def test_router_is_apirouter_instance(self) -> None:
        from provide.uterm.server.routes.pages import create_page_router

        assert isinstance(create_page_router(), APIRouter)

    # ---- create_page_router: handler bodies (exercise nested code) ----------

    async def test_operator_dashboard_uses_state_principal_and_sets_cookies(self) -> None:
        from provide.uterm.server.routes.pages import create_page_router

        endpoint = _endpoint(create_page_router(), "/")
        cfg = _pages_cfg(mode="jwt")
        principal = SimpleNamespace(name="bob")
        req = _request(
            app_state={"uterm_config": cfg},
            state={"uterm_principal": principal},
            headers={"x-forwarded-proto": "https"},
        )
        with (
            patch("provide.uterm.server.routes.pages.operator_dashboard_html", return_value="<html>") as html,
            patch("provide.uterm.server.routes.pages._set_page_cookies") as spc,
            patch("provide.uterm.server.routes.pages.resolve_http_principal") as rhp,
        ):
            resp = await endpoint(req)
        html.assert_called_once()
        rhp.assert_not_called()
        spc.assert_called_once()
        args, kwargs = spc.call_args
        assert args[0] is resp
        assert args[3] == "bob"
        assert args[4] == "operator"
        assert kwargs["secure"] is True

    async def test_operator_dashboard_resolves_principal_when_state_missing(self) -> None:
        from provide.uterm.server.routes.pages import create_page_router

        endpoint = _endpoint(create_page_router(), "/")
        cfg = _pages_cfg(mode="jwt")
        req = _request(app_state={"uterm_config": cfg}, state={"uterm_principal": None}, url_scheme="http")
        resolved = SimpleNamespace(name="resolved-user")
        with (
            patch("provide.uterm.server.routes.pages.operator_dashboard_html", return_value="<html>"),
            patch("provide.uterm.server.routes.pages._set_page_cookies") as spc,
            patch("provide.uterm.server.routes.pages.resolve_http_principal", AsyncMock(return_value=resolved)) as rhp,
        ):
            await endpoint(req)
        rhp.assert_awaited_once_with(req, cfg.auth)
        assert spc.call_args.args[3] == "resolved-user"
        assert spc.call_args.kwargs["secure"] is False

    async def test_session_view_404_when_missing(self) -> None:
        from provide.uterm.server.routes.pages import create_page_router

        endpoint = _endpoint(create_page_router(), "/session/{session_id}")
        reg = MagicMock()
        reg.get_definition = AsyncMock(return_value=None)
        req = _request(app_state={"uterm_registry": reg, "uterm_config": _pages_cfg()})
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "ghost")
        assert exc.value.status_code == 404
        assert exc.value.detail == "unknown session: ghost"

    async def test_session_view_403_when_unauthorized(self) -> None:
        from provide.uterm.server.routes.pages import create_page_router

        endpoint = _endpoint(create_page_router(), "/session/{session_id}")
        reg = MagicMock()
        reg.get_definition = AsyncMock(return_value=SimpleNamespace(display_name="S"))
        authz = MagicMock()
        authz.can_read_session = AsyncMock(return_value=False)
        req = _request(
            app_state={"uterm_registry": reg, "uterm_config": _pages_cfg(), "uterm_authz": authz},
            state={"uterm_principal": SimpleNamespace(name="p")},
        )
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "s1")
        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"

    async def test_session_view_200_passes_operator_false_and_user_surface(self) -> None:
        from provide.uterm.server.routes.pages import create_page_router

        endpoint = _endpoint(create_page_router(), "/session/{session_id}")
        reg = MagicMock()
        reg.get_definition = AsyncMock(return_value=SimpleNamespace(display_name="Disp"))
        authz = MagicMock()
        authz.can_read_session = AsyncMock(return_value=True)
        req = _request(
            app_state={"uterm_registry": reg, "uterm_config": _pages_cfg(), "uterm_authz": authz},
            state={"uterm_principal": SimpleNamespace(name="alice"), "uterm_share_role": "viewer"},
            url_scheme="https",
        )
        with (
            patch("provide.uterm.server.routes.pages.session_page_html", return_value="<h>") as html,
            patch("provide.uterm.server.routes.pages._set_page_cookies") as spc,
        ):
            await endpoint(req, "s1")
        assert html.call_args.kwargs["operator"] is False
        assert html.call_args.kwargs["share_role"] == "viewer"
        assert spc.call_args.args[4] == "user"
        assert spc.call_args.kwargs["secure"] is True
        assert spc.call_args.kwargs["session_id"] == "s1"

    async def test_operator_session_200_passes_operator_true_and_operator_surface(self) -> None:
        from provide.uterm.server.routes.pages import create_page_router

        endpoint = _endpoint(create_page_router(), "/operator/{session_id}")
        reg = MagicMock()
        reg.get_definition = AsyncMock(return_value=SimpleNamespace(display_name="Disp"))
        authz = MagicMock()
        authz.can_read_session = AsyncMock(return_value=True)
        req = _request(
            app_state={"uterm_registry": reg, "uterm_config": _pages_cfg(), "uterm_authz": authz},
            state={"uterm_principal": SimpleNamespace(name="alice")},
        )
        with (
            patch("provide.uterm.server.routes.pages.session_page_html", return_value="<h>") as html,
            patch("provide.uterm.server.routes.pages._set_page_cookies") as spc,
        ):
            await endpoint(req, "s2")
        assert html.call_args.kwargs["operator"] is True
        assert spc.call_args.args[4] == "operator"
        assert spc.call_args.kwargs["session_id"] == "s2"

    async def test_operator_session_404_when_missing(self) -> None:
        from provide.uterm.server.routes.pages import create_page_router

        endpoint = _endpoint(create_page_router(), "/operator/{session_id}")
        reg = MagicMock()
        reg.get_definition = AsyncMock(return_value=None)
        req = _request(app_state={"uterm_registry": reg, "uterm_config": _pages_cfg()})
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "ghost")
        assert exc.value.status_code == 404
        assert exc.value.detail == "unknown session: ghost"

    async def test_operator_session_403_when_unauthorized(self) -> None:
        from provide.uterm.server.routes.pages import create_page_router

        endpoint = _endpoint(create_page_router(), "/operator/{session_id}")
        reg = MagicMock()
        reg.get_definition = AsyncMock(return_value=SimpleNamespace(display_name="S"))
        authz = MagicMock()
        authz.can_read_session = AsyncMock(return_value=False)
        req = _request(
            app_state={"uterm_registry": reg, "uterm_config": _pages_cfg(), "uterm_authz": authz},
            state={"uterm_principal": SimpleNamespace(name="p")},
        )
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "s1")
        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"

    async def test_replay_view_200_uses_replay_html_and_operator_surface(self) -> None:
        from provide.uterm.server.routes.pages import create_page_router

        endpoint = _endpoint(create_page_router(), "/replay/{session_id}")
        reg = MagicMock()
        reg.get_definition = AsyncMock(return_value=SimpleNamespace(display_name="Disp"))
        authz = MagicMock()
        authz.can_read_session = AsyncMock(return_value=True)
        req = _request(
            app_state={"uterm_registry": reg, "uterm_config": _pages_cfg(), "uterm_authz": authz},
            state={"uterm_principal": SimpleNamespace(name="alice")},
        )
        with (
            patch("provide.uterm.server.routes.pages.replay_page_html", return_value="<h>") as html,
            patch("provide.uterm.server.routes.pages._set_page_cookies") as spc,
        ):
            await endpoint(req, "s3")
        html.assert_called_once()
        assert spc.call_args.args[4] == "operator"
        assert spc.call_args.kwargs["session_id"] == "s3"

    async def test_replay_view_404_when_missing(self) -> None:
        from provide.uterm.server.routes.pages import create_page_router

        endpoint = _endpoint(create_page_router(), "/replay/{session_id}")
        reg = MagicMock()
        reg.get_definition = AsyncMock(return_value=None)
        req = _request(app_state={"uterm_registry": reg, "uterm_config": _pages_cfg()})
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "ghost")
        assert exc.value.status_code == 404
        assert exc.value.detail == "unknown session: ghost"

    async def test_replay_view_403_when_unauthorized(self) -> None:
        from provide.uterm.server.routes.pages import create_page_router

        endpoint = _endpoint(create_page_router(), "/replay/{session_id}")
        reg = MagicMock()
        reg.get_definition = AsyncMock(return_value=SimpleNamespace(display_name="S"))
        authz = MagicMock()
        authz.can_read_session = AsyncMock(return_value=False)
        req = _request(
            app_state={"uterm_registry": reg, "uterm_config": _pages_cfg(), "uterm_authz": authz},
            state={"uterm_principal": SimpleNamespace(name="p")},
        )
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "s1")
        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"

    async def test_inspect_view_200_uses_inspect_html_and_operator_surface(self) -> None:
        from provide.uterm.server.routes.pages import create_page_router

        endpoint = _endpoint(create_page_router(), "/inspect/{session_id}")
        reg = MagicMock()
        reg.get_definition = AsyncMock(return_value=SimpleNamespace(display_name="Disp"))
        authz = MagicMock()
        authz.can_read_session = AsyncMock(return_value=True)
        req = _request(
            app_state={"uterm_registry": reg, "uterm_config": _pages_cfg(), "uterm_authz": authz},
            state={"uterm_principal": SimpleNamespace(name="alice")},
        )
        with (
            patch("provide.uterm.server.routes.pages.inspect_page_html", return_value="<h>") as html,
            patch("provide.uterm.server.routes.pages._set_page_cookies") as spc,
        ):
            await endpoint(req, "s4")
        html.assert_called_once()
        assert spc.call_args.args[4] == "operator"
        assert spc.call_args.kwargs["session_id"] == "s4"

    async def test_inspect_view_404_when_missing(self) -> None:
        from provide.uterm.server.routes.pages import create_page_router

        endpoint = _endpoint(create_page_router(), "/inspect/{session_id}")
        reg = MagicMock()
        reg.get_definition = AsyncMock(return_value=None)
        req = _request(app_state={"uterm_registry": reg, "uterm_config": _pages_cfg()})
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "ghost")
        assert exc.value.status_code == 404
        assert exc.value.detail == "unknown session: ghost"

    async def test_inspect_view_403_when_unauthorized(self) -> None:
        from provide.uterm.server.routes.pages import create_page_router

        endpoint = _endpoint(create_page_router(), "/inspect/{session_id}")
        reg = MagicMock()
        reg.get_definition = AsyncMock(return_value=SimpleNamespace(display_name="S"))
        authz = MagicMock()
        authz.can_read_session = AsyncMock(return_value=False)
        req = _request(
            app_state={"uterm_registry": reg, "uterm_config": _pages_cfg(), "uterm_authz": authz},
            state={"uterm_principal": SimpleNamespace(name="p")},
        )
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "s1")
        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"

    async def test_connect_view_200_uses_connect_html_and_operator_surface(self) -> None:
        from provide.uterm.server.routes.pages import create_page_router

        endpoint = _endpoint(create_page_router(), "/connect")
        cfg = _pages_cfg(mode="jwt")
        req = _request(app_state={"uterm_config": cfg}, state={"uterm_principal": SimpleNamespace(name="carol")})
        with (
            patch("provide.uterm.server.routes.pages.connect_page_html", return_value="<c>") as html,
            patch("provide.uterm.server.routes.pages._set_page_cookies") as spc,
            patch("provide.uterm.server.routes.pages.resolve_http_principal") as rhp,
        ):
            await endpoint(req)
        html.assert_called_once()
        rhp.assert_not_called()
        assert spc.call_args.args[3] == "carol"
        assert spc.call_args.args[4] == "operator"

    async def test_connect_view_resolves_principal_when_state_missing(self) -> None:
        from provide.uterm.server.routes.pages import create_page_router

        endpoint = _endpoint(create_page_router(), "/connect")
        cfg = _pages_cfg(mode="jwt")
        req = _request(app_state={"uterm_config": cfg}, state={"uterm_principal": None})
        resolved = SimpleNamespace(name="cx")
        with (
            patch("provide.uterm.server.routes.pages.connect_page_html", return_value="<c>"),
            patch("provide.uterm.server.routes.pages._set_page_cookies") as spc,
            patch("provide.uterm.server.routes.pages.resolve_http_principal", AsyncMock(return_value=resolved)) as rhp,
        ):
            await endpoint(req)
        rhp.assert_awaited_once_with(req, cfg.auth)
        assert spc.call_args.args[3] == "cx"


# ===========================================================================
# webhooks.py (routes) — create_webhook_router + accessors
# ===========================================================================


def _wh_cfg(
    *,
    webhook_id: str = "wh1",
    session_id: str = "s1",
    url: str = "https://example.com/hook",
    event_types: Any = None,
    pattern: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        webhook_id=webhook_id, session_id=session_id, url=url, event_types=event_types, pattern=pattern
    )


class TestWebhookRoutes:
    _REG = "/sessions/{session_id}/webhooks"
    _DEL = "/sessions/{session_id}/webhooks/{webhook_id}"

    def _router(self) -> APIRouter:
        from provide.uterm.server.routes.webhooks import create_webhook_router

        return create_webhook_router()

    def _state(self, *, registry=None, authz=None, principal=None, webhooks=None, hub=None) -> MagicMock:
        app_state = {
            "uterm_registry": registry,
            "uterm_authz": authz,
            "uterm_webhooks": webhooks,
            "uterm_hub": hub if hub is not None else SimpleNamespace(event_bus="bus-sentinel"),
        }
        return _request(app_state=app_state, state={"uterm_principal": principal})

    def test_registry_returns_app_state_object(self) -> None:
        from provide.uterm.server.routes.webhooks import _registry

        reg = object()
        assert _registry(_request(app_state={"uterm_registry": reg})) is reg

    def test_authz_returns_app_state_object(self) -> None:
        from provide.uterm.server.routes.webhooks import _authz

        az = object()
        assert _authz(_request(app_state={"uterm_authz": az})) is az

    def test_principal_present_returned(self) -> None:
        from provide.uterm.server.routes.webhooks import _principal

        p = object()
        assert _principal(_request(state={"uterm_principal": p})) is p

    def test_principal_missing_raises_500(self) -> None:
        from provide.uterm.server.routes.webhooks import _principal

        with pytest.raises(HTTPException) as exc:
            _principal(_request(state={"uterm_principal": None}))
        assert exc.value.status_code == 500
        assert exc.value.detail == "principal was not resolved"

    def test_webhook_manager_present_returned(self) -> None:
        from provide.uterm.server.routes.webhooks import _webhook_manager

        mgr = object()
        assert _webhook_manager(_request(app_state={"uterm_webhooks": mgr})) is mgr

    def test_webhook_manager_missing_raises_503(self) -> None:
        from provide.uterm.server.routes.webhooks import _webhook_manager

        with pytest.raises(HTTPException) as exc:
            _webhook_manager(_request(app_state={"uterm_webhooks": None}))
        assert exc.value.status_code == 503
        assert exc.value.detail == "webhook manager not available"

    def test_router_exposes_all_three_paths(self) -> None:
        router = self._router()
        assert isinstance(router, APIRouter)
        assert _paths(router) == {self._REG, self._DEL}
        assert callable(_endpoint(router, self._REG, "POST"))
        assert callable(_endpoint(router, self._REG, "GET"))
        assert callable(_endpoint(router, self._DEL, "DELETE"))

    async def test_register_ok(self) -> None:
        register = _endpoint(self._router(), self._REG, "POST")
        authz = MagicMock()
        authz.can_mutate_session = AsyncMock(return_value=True)
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=SimpleNamespace())
        mgr = MagicMock()
        mgr.validate_url = MagicMock(return_value="https://example.com/clean")
        mgr.validate_pattern = MagicMock(return_value=r"\$ ")
        mgr.register = AsyncMock(
            return_value=_wh_cfg(
                webhook_id="wh-77", url="https://example.com/clean", event_types=("snapshot", "output"), pattern=r"\$ "
            )
        )
        req = self._state(registry=registry, authz=authz, principal=object(), webhooks=mgr)
        out = await register(
            req,
            "s1",
            {
                "url": "https://example.com/raw",
                "event_types": ["snapshot"],
                "pattern": r"\$ ",
                "secret": "sek",  # pragma: allowlist secret
            },  # pragma: allowlist secret
        )
        assert out == {
            "webhook_id": "wh-77",
            "session_id": "s1",
            "url": "https://example.com/clean",
            "event_types": ["snapshot", "output"],
            "pattern": r"\$ ",
        }
        registry.get_definition.assert_awaited_once_with("s1")
        authz.can_mutate_session.assert_awaited_once_with(
            req.state.uterm_principal,
            req.app.state.uterm_registry.get_definition.return_value,
            "session.control.update",
        )
        mgr.validate_url.assert_called_once_with("https://example.com/raw")
        mgr.register.assert_awaited_once_with(
            "s1",
            "https://example.com/clean",
            event_types=["snapshot"],
            pattern=r"\$ ",
            secret="sek",  # pragma: allowlist secret
            event_bus="bus-sentinel",
        )

    async def test_register_event_types_none(self) -> None:
        register = _endpoint(self._router(), self._REG, "POST")
        authz = MagicMock()
        authz.can_mutate_session = AsyncMock(return_value=True)
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=SimpleNamespace())
        mgr = MagicMock()
        mgr.validate_url = MagicMock(return_value="https://example.com/clean")
        mgr.validate_pattern = MagicMock(return_value=None)
        mgr.register = AsyncMock(return_value=_wh_cfg(event_types=None, pattern=None, url="https://example.com/clean"))
        req = self._state(registry=registry, authz=authz, principal=object(), webhooks=mgr)
        out = await register(req, "s1", {"url": "https://example.com/raw"})
        assert out["event_types"] is None
        assert out["pattern"] is None
        mgr.register.assert_awaited_once_with(
            "s1",
            "https://example.com/clean",
            event_types=None,
            pattern=None,
            secret=None,
            event_bus="bus-sentinel",
        )

    async def test_register_unknown_session_404(self) -> None:
        register = _endpoint(self._router(), self._REG, "POST")
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=None)
        authz = MagicMock()
        authz.can_mutate_session = AsyncMock(return_value=True)
        req = self._state(registry=registry, authz=authz, principal=object(), webhooks=MagicMock())
        with pytest.raises(HTTPException) as exc:
            await register(req, "ghost", {"url": "https://example.com/hook"})
        assert exc.value.status_code == 404
        assert exc.value.detail == "unknown session: ghost"
        authz.can_mutate_session.assert_not_awaited()

    async def test_register_unauthorized_403(self) -> None:
        register = _endpoint(self._router(), self._REG, "POST")
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=SimpleNamespace())
        authz = MagicMock()
        authz.can_mutate_session = AsyncMock(return_value=False)
        mgr = MagicMock()
        mgr.register = AsyncMock()
        req = self._state(registry=registry, authz=authz, principal=object(), webhooks=mgr)
        with pytest.raises(HTTPException) as exc:
            await register(req, "s1", {"url": "https://example.com/hook"})
        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"
        mgr.register.assert_not_awaited()

    async def test_register_missing_url_422(self) -> None:
        register = _endpoint(self._router(), self._REG, "POST")
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=SimpleNamespace())
        authz = MagicMock()
        authz.can_mutate_session = AsyncMock(return_value=True)
        req = self._state(registry=registry, authz=authz, principal=object(), webhooks=MagicMock())
        with pytest.raises(HTTPException) as exc:
            await register(req, "s1", {"event_types": ["snapshot"]})
        assert exc.value.status_code == 422
        assert exc.value.detail == "url is required"

    async def test_register_non_string_url_422(self) -> None:
        register = _endpoint(self._router(), self._REG, "POST")
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=SimpleNamespace())
        authz = MagicMock()
        authz.can_mutate_session = AsyncMock(return_value=True)
        req = self._state(registry=registry, authz=authz, principal=object(), webhooks=MagicMock())
        with pytest.raises(HTTPException) as exc:
            await register(req, "s1", {"url": 123})
        assert exc.value.status_code == 422
        assert exc.value.detail == "url is required"

    async def test_register_invalid_url_422(self) -> None:
        register = _endpoint(self._router(), self._REG, "POST")
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=SimpleNamespace())
        authz = MagicMock()
        authz.can_mutate_session = AsyncMock(return_value=True)
        mgr = MagicMock()
        mgr.validate_url = MagicMock(side_effect=ValueError("scheme not allowed"))
        req = self._state(registry=registry, authz=authz, principal=object(), webhooks=mgr)
        with pytest.raises(HTTPException) as exc:
            await register(req, "s1", {"url": "ftp://bad"})
        assert exc.value.status_code == 422
        assert exc.value.detail == "scheme not allowed"

    async def test_register_bad_event_types_422(self) -> None:
        register = _endpoint(self._router(), self._REG, "POST")
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=SimpleNamespace())
        authz = MagicMock()
        authz.can_mutate_session = AsyncMock(return_value=True)
        mgr = MagicMock()
        mgr.validate_url = MagicMock(return_value="https://example.com/clean")
        req = self._state(registry=registry, authz=authz, principal=object(), webhooks=mgr)
        with pytest.raises(HTTPException) as exc:
            await register(req, "s1", {"url": "https://example.com/hook", "event_types": "snapshot"})
        assert exc.value.status_code == 422
        assert exc.value.detail == "event_types must be a list"

    async def test_register_invalid_pattern_422(self) -> None:
        register = _endpoint(self._router(), self._REG, "POST")
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=SimpleNamespace())
        authz = MagicMock()
        authz.can_mutate_session = AsyncMock(return_value=True)
        mgr = MagicMock()
        mgr.validate_url = MagicMock(return_value="https://example.com/clean")
        mgr.validate_pattern = MagicMock(side_effect=ValueError("bad regex"))
        req = self._state(registry=registry, authz=authz, principal=object(), webhooks=mgr)
        with pytest.raises(HTTPException) as exc:
            await register(req, "s1", {"url": "https://example.com/hook", "pattern": "("})
        assert exc.value.status_code == 422
        assert exc.value.detail == "bad regex"

    async def test_list_returns_rows(self) -> None:
        list_wh = _endpoint(self._router(), self._REG, "GET")
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=SimpleNamespace())
        authz = MagicMock()
        authz.can_mutate_session = AsyncMock(return_value=True)
        mgr = MagicMock()
        mgr.list_webhooks = MagicMock(
            return_value=[
                _wh_cfg(webhook_id="a", url="https://example.com/a", event_types=["snapshot"], pattern="p"),
                _wh_cfg(webhook_id="b", url="https://example.com/b", event_types=None, pattern=None),
            ]
        )
        req = self._state(registry=registry, authz=authz, principal=object(), webhooks=mgr)
        out = await list_wh(req, "s1")
        assert out == {
            "webhooks": [
                {
                    "webhook_id": "a",
                    "session_id": "s1",
                    "url": "https://example.com/a",
                    "event_types": ["snapshot"],
                    "pattern": "p",
                },
                {
                    "webhook_id": "b",
                    "session_id": "s1",
                    "url": "https://example.com/b",
                    "event_types": None,
                    "pattern": None,
                },
            ]
        }
        mgr.list_webhooks.assert_called_once_with("s1")

    async def test_list_empty(self) -> None:
        list_wh = _endpoint(self._router(), self._REG, "GET")
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=SimpleNamespace())
        authz = MagicMock()
        authz.can_mutate_session = AsyncMock(return_value=True)
        mgr = MagicMock()
        mgr.list_webhooks = MagicMock(return_value=[])
        req = self._state(registry=registry, authz=authz, principal=object(), webhooks=mgr)
        assert await list_wh(req, "s1") == {"webhooks": []}

    async def test_list_unknown_session_404(self) -> None:
        list_wh = _endpoint(self._router(), self._REG, "GET")
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=None)
        authz = MagicMock()
        authz.can_mutate_session = AsyncMock(return_value=True)
        req = self._state(registry=registry, authz=authz, principal=object(), webhooks=MagicMock())
        with pytest.raises(HTTPException) as exc:
            await list_wh(req, "ghost")
        assert exc.value.status_code == 404
        assert exc.value.detail == "unknown session: ghost"

    async def test_list_unauthorized_403(self) -> None:
        list_wh = _endpoint(self._router(), self._REG, "GET")
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=SimpleNamespace())
        authz = MagicMock()
        authz.can_mutate_session = AsyncMock(return_value=False)
        mgr = MagicMock()
        mgr.list_webhooks = MagicMock()
        req = self._state(registry=registry, authz=authz, principal=object(), webhooks=mgr)
        with pytest.raises(HTTPException) as exc:
            await list_wh(req, "s1")
        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"
        mgr.list_webhooks.assert_not_called()

    async def test_unregister_ok(self) -> None:
        unregister = _endpoint(self._router(), self._DEL, "DELETE")
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=SimpleNamespace())
        authz = MagicMock()
        authz.can_mutate_session = AsyncMock(return_value=True)
        mgr = MagicMock()
        mgr.get_webhook = MagicMock(return_value=_wh_cfg(webhook_id="wh1", session_id="s1"))
        mgr.unregister = AsyncMock(return_value=True)
        req = self._state(registry=registry, authz=authz, principal=object(), webhooks=mgr)
        out = await unregister(req, "s1", "wh1")
        assert out == {"ok": True, "webhook_id": "wh1"}
        mgr.get_webhook.assert_called_once_with("wh1")
        mgr.unregister.assert_awaited_once_with("wh1")

    async def test_unregister_unknown_webhook_404(self) -> None:
        unregister = _endpoint(self._router(), self._DEL, "DELETE")
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=SimpleNamespace())
        authz = MagicMock()
        authz.can_mutate_session = AsyncMock(return_value=True)
        mgr = MagicMock()
        mgr.get_webhook = MagicMock(return_value=None)
        mgr.unregister = AsyncMock()
        req = self._state(registry=registry, authz=authz, principal=object(), webhooks=mgr)
        with pytest.raises(HTTPException) as exc:
            await unregister(req, "s1", "ghost")
        assert exc.value.status_code == 404
        assert exc.value.detail == "unknown webhook: ghost"
        mgr.unregister.assert_not_awaited()

    async def test_unregister_session_mismatch_404(self) -> None:
        unregister = _endpoint(self._router(), self._DEL, "DELETE")
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=SimpleNamespace())
        authz = MagicMock()
        authz.can_mutate_session = AsyncMock(return_value=True)
        mgr = MagicMock()
        mgr.get_webhook = MagicMock(return_value=_wh_cfg(webhook_id="wh1", session_id="other"))
        mgr.unregister = AsyncMock()
        req = self._state(registry=registry, authz=authz, principal=object(), webhooks=mgr)
        with pytest.raises(HTTPException) as exc:
            await unregister(req, "s1", "wh1")
        assert exc.value.status_code == 404
        assert exc.value.detail == "unknown webhook: wh1"
        mgr.unregister.assert_not_awaited()

    async def test_unregister_unknown_session_404(self) -> None:
        unregister = _endpoint(self._router(), self._DEL, "DELETE")
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=None)
        authz = MagicMock()
        authz.can_mutate_session = AsyncMock(return_value=True)
        req = self._state(registry=registry, authz=authz, principal=object(), webhooks=MagicMock())
        with pytest.raises(HTTPException) as exc:
            await unregister(req, "ghost", "wh1")
        assert exc.value.status_code == 404
        assert exc.value.detail == "unknown session: ghost"

    async def test_unregister_unauthorized_403(self) -> None:
        unregister = _endpoint(self._router(), self._DEL, "DELETE")
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=SimpleNamespace())
        authz = MagicMock()
        authz.can_mutate_session = AsyncMock(return_value=False)
        mgr = MagicMock()
        mgr.get_webhook = MagicMock()
        mgr.unregister = AsyncMock()
        req = self._state(registry=registry, authz=authz, principal=object(), webhooks=mgr)
        with pytest.raises(HTTPException) as exc:
            await unregister(req, "s1", "wh1")
        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"
        mgr.get_webhook.assert_not_called()
        mgr.unregister.assert_not_awaited()


# ===========================================================================
# routes/profiles.py
# ===========================================================================


class TestProfilesRoutes:
    _ROOT = "/api/profiles"
    _ITEM = "/api/profiles/{profile_id}"
    _CONNECT = "/api/profiles/{profile_id}/connect"

    def _router(self) -> APIRouter:
        from provide.uterm.server.routes.profiles import create_profiles_router

        return create_profiles_router()

    def _profile(
        self,
        *,
        profile_id: str = "p1",
        owner: str = "alice",
        name: str = "My Server",
        connector_type: str = "ssh",
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        tags: tuple[str, ...] = (),
        input_mode: str = "open",
        recording_enabled: bool = False,
        visibility: str = "private",
        dump: dict[str, Any] | None = None,
    ) -> MagicMock:
        prof = MagicMock(name="profile")
        prof.profile_id = profile_id
        prof.owner = owner
        prof.name = name
        prof.connector_type = connector_type
        prof.host = host
        prof.port = port
        prof.username = username
        prof.tags = list(tags)
        prof.input_mode = input_mode
        prof.recording_enabled = recording_enabled
        prof.visibility = visibility
        prof.model_dump = MagicMock(return_value=dump if dump is not None else {"profile_id": profile_id})
        return prof

    # ---- module accessors ---------------------------------------------------

    def test_store_returns_app_state_object(self) -> None:
        from provide.uterm.server.routes.profiles import _store

        store = object()
        assert _store(_request(app_state={"uterm_profile_store": store})) is store

    def test_authz_returns_app_state_object(self) -> None:
        from provide.uterm.server.routes.profiles import _authz

        az = object()
        assert _authz(_request(app_state={"uterm_authz": az})) is az

    def test_registry_returns_app_state_object(self) -> None:
        from provide.uterm.server.routes.profiles import _registry

        reg = object()
        assert _registry(_request(app_state={"uterm_registry": reg})) is reg

    def test_principal_present_returned(self) -> None:
        from provide.uterm.server.routes.profiles import _principal

        p = object()
        assert _principal(_request(state={"uterm_principal": p})) is p

    def test_principal_missing_raises_500(self) -> None:
        from provide.uterm.server.routes.profiles import _principal

        with pytest.raises(HTTPException) as exc:
            _principal(_request(state={"uterm_principal": None}))
        assert exc.value.status_code == 500
        assert exc.value.detail == "principal was not resolved"

    def test_not_found_builds_404(self) -> None:
        from provide.uterm.server.routes.profiles import _not_found

        exc = _not_found("ghost")
        assert isinstance(exc, HTTPException)
        assert exc.status_code == 404
        assert exc.detail == "unknown profile: ghost"

    # ---- create_profiles_router: structure ----------------------------------

    def test_router_is_apirouter_with_all_paths(self) -> None:
        router = self._router()
        assert isinstance(router, APIRouter)
        assert _paths(router) == {self._ROOT, self._ITEM, self._CONNECT}
        assert callable(_endpoint(router, self._ROOT, "GET"))
        assert callable(_endpoint(router, self._ROOT, "POST"))
        assert callable(_endpoint(router, self._ITEM, "GET"))
        assert callable(_endpoint(router, self._ITEM, "PUT"))
        assert callable(_endpoint(router, self._ITEM, "DELETE"))
        assert callable(_endpoint(router, self._CONNECT, "POST"))

    # ---- list_profiles ------------------------------------------------------

    async def test_list_admin_lists_all(self) -> None:
        endpoint = _endpoint(self._router(), self._ROOT, "GET")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=True)
        prof = self._profile(dump={"profile_id": "p1"})
        store = MagicMock()
        store.list_profiles = AsyncMock(return_value=[prof])
        principal = SimpleNamespace(subject_id="alice")
        req = _request(
            app_state={"uterm_authz": authz, "uterm_profile_store": store},
            state={"uterm_principal": principal},
        )
        out = await endpoint(req)
        assert out == [{"profile_id": "p1"}]
        authz.is_admin.assert_awaited_once_with(principal)
        store.list_profiles.assert_awaited_once_with()
        prof.model_dump.assert_called_once_with(mode="python")

    async def test_list_non_admin_filters_by_owner(self) -> None:
        endpoint = _endpoint(self._router(), self._ROOT, "GET")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=False)
        store = MagicMock()
        store.list_profiles = AsyncMock(return_value=[])
        principal = SimpleNamespace(subject_id="bob")
        req = _request(
            app_state={"uterm_authz": authz, "uterm_profile_store": store},
            state={"uterm_principal": principal},
        )
        out = await endpoint(req)
        assert out == []
        store.list_profiles.assert_awaited_once_with(owner="bob")

    # ---- get_profile --------------------------------------------------------

    async def test_get_profile_ok(self) -> None:
        endpoint = _endpoint(self._router(), self._ITEM, "GET")
        prof = self._profile(dump={"profile_id": "p1", "name": "X"})
        store = MagicMock()
        store.get_profile = AsyncMock(return_value=prof)
        authz = MagicMock()
        authz.can_read_profile = AsyncMock(return_value=True)
        principal = SimpleNamespace(subject_id="alice")
        req = _request(
            app_state={"uterm_authz": authz, "uterm_profile_store": store},
            state={"uterm_principal": principal},
        )
        out = await endpoint(req, "p1")
        assert out == {"profile_id": "p1", "name": "X"}
        store.get_profile.assert_awaited_once_with("p1")
        authz.can_read_profile.assert_awaited_once_with(principal, prof)
        prof.model_dump.assert_called_once_with(mode="python")

    async def test_get_profile_unknown_404(self) -> None:
        endpoint = _endpoint(self._router(), self._ITEM, "GET")
        store = MagicMock()
        store.get_profile = AsyncMock(return_value=None)
        authz = MagicMock()
        authz.can_read_profile = AsyncMock(return_value=True)
        req = _request(
            app_state={"uterm_authz": authz, "uterm_profile_store": store},
            state={"uterm_principal": object()},
        )
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "ghost")
        assert exc.value.status_code == 404
        assert exc.value.detail == "unknown profile: ghost"
        authz.can_read_profile.assert_not_awaited()

    async def test_get_profile_forbidden_403(self) -> None:
        endpoint = _endpoint(self._router(), self._ITEM, "GET")
        prof = self._profile()
        store = MagicMock()
        store.get_profile = AsyncMock(return_value=prof)
        authz = MagicMock()
        authz.can_read_profile = AsyncMock(return_value=False)
        req = _request(
            app_state={"uterm_authz": authz, "uterm_profile_store": store},
            state={"uterm_principal": object()},
        )
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "p1")
        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"
        prof.model_dump.assert_not_called()

    # ---- create_profile -----------------------------------------------------

    async def test_create_profile_forbidden_403(self) -> None:
        endpoint = _endpoint(self._router(), self._ROOT, "POST")
        authz = MagicMock()
        authz.can_create_session = AsyncMock(return_value=False)
        store = MagicMock()
        store.create_profile = AsyncMock()
        principal = SimpleNamespace(subject_id="bob")
        req = _request(
            app_state={"uterm_authz": authz, "uterm_profile_store": store},
            state={"uterm_principal": principal},
        )
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, {"name": "X"})
        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"
        authz.can_create_session.assert_awaited_once_with(principal)
        store.create_profile.assert_not_awaited()

    async def test_create_profile_full_payload(self) -> None:
        endpoint = _endpoint(self._router(), self._ROOT, "POST")
        authz = MagicMock()
        authz.can_create_session = AsyncMock(return_value=True)
        store = MagicMock()
        store.create_profile = AsyncMock(side_effect=lambda p: p)
        principal = SimpleNamespace(subject_id="alice")
        req = _request(
            app_state={"uterm_authz": authz, "uterm_profile_store": store},
            state={"uterm_principal": principal},
        )
        payload = {
            "name": "  Prod  ",
            "connector_type": "telnet",
            "host": "  host.example  ",
            "port": "2222",
            "username": "  joe  ",
            "tags": ["  a  ", "", "b", 7],
            "input_mode": "hijack",
            "recording_enabled": 1,
            "visibility": "shared",
        }
        out = await endpoint(req, payload)
        assert out["owner"] == "alice"
        assert out["name"] == "Prod"
        assert out["connector_type"] == "telnet"
        assert out["host"] == "host.example"
        assert out["port"] == 2222
        assert out["username"] == "joe"
        assert out["tags"] == ["a", "b", "7"]
        assert out["input_mode"] == "hijack"
        assert out["recording_enabled"] is True
        assert out["visibility"] == "shared"
        assert out["profile_id"].startswith("profile-")
        assert out["created_at"] == out["updated_at"]
        created_arg = store.create_profile.await_args.args[0]
        assert created_arg.owner == "alice"

    async def test_create_profile_defaults_and_non_list_tags(self) -> None:
        endpoint = _endpoint(self._router(), self._ROOT, "POST")
        authz = MagicMock()
        authz.can_create_session = AsyncMock(return_value=True)
        store = MagicMock()
        store.create_profile = AsyncMock(side_effect=lambda p: p)
        principal = SimpleNamespace(subject_id="alice")
        req = _request(
            app_state={"uterm_authz": authz, "uterm_profile_store": store},
            state={"uterm_principal": principal},
        )
        out = await endpoint(req, {"tags": "not-a-list"})
        assert out["name"] == "Unnamed"
        assert out["connector_type"] == "ssh"
        assert out["host"] is None
        assert out["port"] is None
        assert out["username"] is None
        assert out["tags"] == []
        assert out["input_mode"] == "open"
        assert out["recording_enabled"] is False
        assert out["visibility"] == "private"

    async def test_create_profile_blank_name_becomes_unnamed(self) -> None:
        endpoint = _endpoint(self._router(), self._ROOT, "POST")
        authz = MagicMock()
        authz.can_create_session = AsyncMock(return_value=True)
        store = MagicMock()
        store.create_profile = AsyncMock(side_effect=lambda p: p)
        req = _request(
            app_state={"uterm_authz": authz, "uterm_profile_store": store},
            state={"uterm_principal": SimpleNamespace(subject_id="alice")},
        )
        out = await endpoint(req, {"name": ""})
        assert out["name"] == "Unnamed"

    # ---- update_profile -----------------------------------------------------

    async def test_update_profile_ok(self) -> None:
        endpoint = _endpoint(self._router(), self._ITEM, "PUT")
        prof = self._profile()
        updated = self._profile(dump={"profile_id": "p1", "name": "Renamed"})
        store = MagicMock()
        store.get_profile = AsyncMock(return_value=prof)
        store.update_profile = AsyncMock(return_value=updated)
        authz = MagicMock()
        authz.can_mutate_profile = AsyncMock(return_value=True)
        principal = SimpleNamespace(subject_id="alice")
        req = _request(
            app_state={"uterm_authz": authz, "uterm_profile_store": store},
            state={"uterm_principal": principal},
        )
        out = await endpoint(req, "p1", {"name": "Renamed", "bogus": "ignored", "host": "h"})
        assert out == {"profile_id": "p1", "name": "Renamed"}
        authz.can_mutate_profile.assert_awaited_once_with(principal, prof)
        store.update_profile.assert_awaited_once_with("p1", {"name": "Renamed", "host": "h"})
        updated.model_dump.assert_called_once_with(mode="python")

    async def test_update_profile_unknown_404(self) -> None:
        endpoint = _endpoint(self._router(), self._ITEM, "PUT")
        store = MagicMock()
        store.get_profile = AsyncMock(return_value=None)
        store.update_profile = AsyncMock()
        authz = MagicMock()
        authz.can_mutate_profile = AsyncMock(return_value=True)
        req = _request(
            app_state={"uterm_authz": authz, "uterm_profile_store": store},
            state={"uterm_principal": object()},
        )
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "ghost", {"name": "x"})
        assert exc.value.status_code == 404
        assert exc.value.detail == "unknown profile: ghost"
        authz.can_mutate_profile.assert_not_awaited()
        store.update_profile.assert_not_awaited()

    async def test_update_profile_forbidden_403(self) -> None:
        endpoint = _endpoint(self._router(), self._ITEM, "PUT")
        prof = self._profile()
        store = MagicMock()
        store.get_profile = AsyncMock(return_value=prof)
        store.update_profile = AsyncMock()
        authz = MagicMock()
        authz.can_mutate_profile = AsyncMock(return_value=False)
        req = _request(
            app_state={"uterm_authz": authz, "uterm_profile_store": store},
            state={"uterm_principal": object()},
        )
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "p1", {"name": "x"})
        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"
        store.update_profile.assert_not_awaited()

    async def test_update_profile_validation_error_422(self) -> None:
        from pydantic import ValidationError

        endpoint = _endpoint(self._router(), self._ITEM, "PUT")
        prof = self._profile()
        store = MagicMock()
        store.get_profile = AsyncMock(return_value=prof)
        err = ValidationError.from_exception_data("ConnectionProfile", [])
        store.update_profile = AsyncMock(side_effect=err)
        authz = MagicMock()
        authz.can_mutate_profile = AsyncMock(return_value=True)
        req = _request(
            app_state={"uterm_authz": authz, "uterm_profile_store": store},
            state={"uterm_principal": object()},
        )
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "p1", {"name": "x"})
        assert exc.value.status_code == 422
        assert exc.value.detail == str(err)

    async def test_update_profile_store_returns_none_404(self) -> None:
        endpoint = _endpoint(self._router(), self._ITEM, "PUT")
        prof = self._profile()
        store = MagicMock()
        store.get_profile = AsyncMock(return_value=prof)
        store.update_profile = AsyncMock(return_value=None)
        authz = MagicMock()
        authz.can_mutate_profile = AsyncMock(return_value=True)
        req = _request(
            app_state={"uterm_authz": authz, "uterm_profile_store": store},
            state={"uterm_principal": object()},
        )
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "p1", {"name": "x"})
        assert exc.value.status_code == 404
        assert exc.value.detail == "unknown profile: p1"

    # ---- delete_profile -----------------------------------------------------

    async def test_delete_profile_ok(self) -> None:
        endpoint = _endpoint(self._router(), self._ITEM, "DELETE")
        prof = self._profile()
        store = MagicMock()
        store.get_profile = AsyncMock(return_value=prof)
        store.delete_profile = AsyncMock()
        authz = MagicMock()
        authz.can_mutate_profile = AsyncMock(return_value=True)
        principal = SimpleNamespace(subject_id="alice")
        req = _request(
            app_state={"uterm_authz": authz, "uterm_profile_store": store},
            state={"uterm_principal": principal},
        )
        out = await endpoint(req, "p1")
        assert out == {"ok": True}
        authz.can_mutate_profile.assert_awaited_once_with(principal, prof)
        store.delete_profile.assert_awaited_once_with("p1")

    async def test_delete_profile_unknown_404(self) -> None:
        endpoint = _endpoint(self._router(), self._ITEM, "DELETE")
        store = MagicMock()
        store.get_profile = AsyncMock(return_value=None)
        store.delete_profile = AsyncMock()
        authz = MagicMock()
        authz.can_mutate_profile = AsyncMock(return_value=True)
        req = _request(
            app_state={"uterm_authz": authz, "uterm_profile_store": store},
            state={"uterm_principal": object()},
        )
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "ghost")
        assert exc.value.status_code == 404
        assert exc.value.detail == "unknown profile: ghost"
        authz.can_mutate_profile.assert_not_awaited()
        store.delete_profile.assert_not_awaited()

    async def test_delete_profile_forbidden_403(self) -> None:
        endpoint = _endpoint(self._router(), self._ITEM, "DELETE")
        prof = self._profile()
        store = MagicMock()
        store.get_profile = AsyncMock(return_value=prof)
        store.delete_profile = AsyncMock()
        authz = MagicMock()
        authz.can_mutate_profile = AsyncMock(return_value=False)
        req = _request(
            app_state={"uterm_authz": authz, "uterm_profile_store": store},
            state={"uterm_principal": object()},
        )
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "p1")
        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"
        store.delete_profile.assert_not_awaited()

    # ---- connect_from_profile -----------------------------------------------

    def _connect_req(self, *, profile, authz, registry, app_path: str = "/app") -> MagicMock:
        store = MagicMock()
        store.get_profile = AsyncMock(return_value=profile)
        cfg = SimpleNamespace(ui=SimpleNamespace(app_path=app_path))
        return _request(
            app_state={
                "uterm_authz": authz,
                "uterm_profile_store": store,
                "uterm_registry": registry,
                "uterm_config": cfg,
            },
            state={"uterm_principal": SimpleNamespace(subject_id="alice")},
        )

    async def test_connect_ok_full_config(self) -> None:
        import provide.uterm.server.routes.profiles as mod

        endpoint = _endpoint(self._router(), self._CONNECT, "POST")
        prof = self._profile(
            host="h.example",
            port=2222,
            username="joe",
            name="Disp",
            connector_type="ssh",
            tags=("x", "y"),
            input_mode="hijack",
            recording_enabled=True,
        )
        authz = MagicMock()
        authz.can_read_profile = AsyncMock(return_value=True)
        authz.can_create_session = AsyncMock(return_value=True)
        registry = MagicMock()
        registry.create_session = AsyncMock(return_value=SimpleNamespace())
        req = self._connect_req(profile=prof, authz=authz, registry=registry, app_path="/ui")
        with patch.object(mod, "model_dump", return_value={"state": "running"}) as md:
            out = await endpoint(req, "p1", {"password": "sek"})  # pragma: allowlist secret
        assert out["session_id"].startswith("connect-")
        assert out["url"] == f"/ui/session/{out['session_id']}"
        assert out["state"] == "running"
        registry.create_session.assert_awaited_once()
        sent = registry.create_session.await_args.args[0]
        assert sent["display_name"] == "Disp"
        assert sent["connector_type"] == "ssh"
        assert sent["connector_config"] == {
            "host": "h.example",
            "port": 2222,
            "username": "joe",
            "password": "sek",  # pragma: allowlist secret
        }
        assert sent["input_mode"] == "hijack"
        assert sent["tags"] == ["x", "y"]
        assert sent["auto_start"] is True
        assert sent["ephemeral"] is True
        assert sent["visibility"] == "private"
        assert sent["owner"] == "alice"
        assert sent["recording_enabled"] is True
        md.assert_called_once()

    async def test_connect_minimal_config_no_password_no_recording(self) -> None:
        import provide.uterm.server.routes.profiles as mod

        endpoint = _endpoint(self._router(), self._CONNECT, "POST")
        prof = self._profile(
            host=None,
            port=None,
            username=None,
            recording_enabled=False,
            connector_type="ushell",
        )
        authz = MagicMock()
        authz.can_read_profile = AsyncMock(return_value=True)
        authz.can_create_session = AsyncMock(return_value=True)
        registry = MagicMock()
        registry.create_session = AsyncMock(return_value=SimpleNamespace())
        req = self._connect_req(profile=prof, authz=authz, registry=registry)
        with patch.object(mod, "model_dump", return_value={}):
            await endpoint(req, "p1", {})
        sent = registry.create_session.await_args.args[0]
        assert sent["connector_config"] == {}
        assert "recording_enabled" not in sent

    async def test_connect_unknown_profile_404(self) -> None:
        endpoint = _endpoint(self._router(), self._CONNECT, "POST")
        authz = MagicMock()
        authz.can_read_profile = AsyncMock(return_value=True)
        authz.can_create_session = AsyncMock(return_value=True)
        registry = MagicMock()
        registry.create_session = AsyncMock()
        req = self._connect_req(profile=None, authz=authz, registry=registry)
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "ghost", {})
        assert exc.value.status_code == 404
        assert exc.value.detail == "unknown profile: ghost"
        authz.can_read_profile.assert_not_awaited()
        registry.create_session.assert_not_awaited()

    async def test_connect_cannot_read_profile_403(self) -> None:
        endpoint = _endpoint(self._router(), self._CONNECT, "POST")
        prof = self._profile()
        authz = MagicMock()
        authz.can_read_profile = AsyncMock(return_value=False)
        authz.can_create_session = AsyncMock(return_value=True)
        registry = MagicMock()
        registry.create_session = AsyncMock()
        req = self._connect_req(profile=prof, authz=authz, registry=registry)
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "p1", {})
        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"
        authz.can_create_session.assert_not_awaited()
        registry.create_session.assert_not_awaited()

    async def test_connect_cannot_create_session_403(self) -> None:
        endpoint = _endpoint(self._router(), self._CONNECT, "POST")
        prof = self._profile()
        authz = MagicMock()
        authz.can_read_profile = AsyncMock(return_value=True)
        authz.can_create_session = AsyncMock(return_value=False)
        registry = MagicMock()
        registry.create_session = AsyncMock()
        req = self._connect_req(profile=prof, authz=authz, registry=registry)
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "p1", {})
        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"
        registry.create_session.assert_not_awaited()

    async def test_connect_session_validation_error_422(self) -> None:
        from provide.uterm.server.registry import SessionValidationError

        endpoint = _endpoint(self._router(), self._CONNECT, "POST")
        prof = self._profile()
        authz = MagicMock()
        authz.can_read_profile = AsyncMock(return_value=True)
        authz.can_create_session = AsyncMock(return_value=True)
        registry = MagicMock()
        registry.create_session = AsyncMock(side_effect=SessionValidationError("bad payload"))
        req = self._connect_req(profile=prof, authz=authz, registry=registry)
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "p1", {})
        assert exc.value.status_code == 422
        assert exc.value.detail == "bad payload"

    async def test_connect_value_error_409(self) -> None:
        endpoint = _endpoint(self._router(), self._CONNECT, "POST")
        prof = self._profile()
        authz = MagicMock()
        authz.can_read_profile = AsyncMock(return_value=True)
        authz.can_create_session = AsyncMock(return_value=True)
        registry = MagicMock()
        registry.create_session = AsyncMock(side_effect=ValueError("duplicate session"))
        req = self._connect_req(profile=prof, authz=authz, registry=registry)
        with pytest.raises(HTTPException) as exc:
            await endpoint(req, "p1", {})
        assert exc.value.status_code == 409
        assert exc.value.detail == "duplicate session"


# ===========================================================================
# routes/api_keys.py
# ===========================================================================


# ===========================================================================
# api_keys.py — module accessors + create_api_keys_router (nested handlers)
# ===========================================================================


class TestApiKeysRoutes:
    _KEYS = "/keys"
    _KEY = "/keys/{key_id}"

    def _router(self) -> APIRouter:
        from provide.uterm.server.routes.api_keys import create_api_keys_router

        return create_api_keys_router()

    def _state(
        self,
        *,
        authz: Any = None,
        principal: Any = None,
        store: Any = None,
        api_keys_enabled: bool = True,
        client_host: str | None = "1.2.3.4",
    ) -> MagicMock:
        cfg = SimpleNamespace(auth=SimpleNamespace(api_keys_enabled=api_keys_enabled))
        app_state = {"uterm_authz": authz, "uterm_config": cfg, "uterm_api_key_store": store}
        return _request(app_state=app_state, state={"uterm_principal": principal}, client_host=client_host)

    def _record(
        self,
        *,
        key_id: str = "kid-1",
        name: str = "deploy",
        scopes: Any = frozenset({"admin", "viewer"}),
        created_at: float = 100.0,
        expires_at: float | None = 200.0,
        last_used_at: float | None = None,
        revoked: bool = False,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            key_id=key_id,
            name=name,
            scopes=scopes,
            created_at=created_at,
            expires_at=expires_at,
            last_used_at=last_used_at,
            revoked=revoked,
        )

    # ---- module accessors ---------------------------------------------------

    def test_principal_present_returned(self) -> None:
        from provide.uterm.server.routes.api_keys import _principal

        p = object()
        assert _principal(_request(state={"uterm_principal": p})) is p

    def test_principal_missing_raises_500(self) -> None:
        from provide.uterm.server.routes.api_keys import _principal

        with pytest.raises(HTTPException) as exc:
            _principal(_request(state={"uterm_principal": None}))
        assert exc.value.status_code == 500
        assert exc.value.detail == "principal was not resolved"

    def test_authz_returns_app_state_object(self) -> None:
        from provide.uterm.server.routes.api_keys import _authz

        az = object()
        assert _authz(_request(app_state={"uterm_authz": az})) is az

    def test_source_ip_from_client_host(self) -> None:
        from provide.uterm.server.routes.api_keys import _source_ip

        assert _source_ip(_request(client_host="9.9.9.9")) == "9.9.9.9"

    def test_source_ip_no_client_is_unknown(self) -> None:
        from provide.uterm.server.routes.api_keys import _source_ip

        assert _source_ip(_request(client_host=None)) == "unknown"

    def test_source_ip_client_without_host_is_unknown(self) -> None:
        from provide.uterm.server.routes.api_keys import _source_ip

        req = _request()
        req.client = SimpleNamespace()  # client present but no .host
        assert _source_ip(req) == "unknown"

    # ---- create_api_keys_router: structure ----------------------------------

    def test_router_is_apirouter_with_expected_routes(self) -> None:
        router = self._router()
        assert isinstance(router, APIRouter)
        assert _paths(router) == {self._KEYS, self._KEY}
        assert callable(_endpoint(router, self._KEYS, "POST"))
        assert callable(_endpoint(router, self._KEYS, "GET"))
        assert callable(_endpoint(router, self._KEY, "DELETE"))

    # ---- create_api_key -----------------------------------------------------

    async def test_create_ok_returns_record_and_audits(self) -> None:
        create = _endpoint(self._router(), self._KEYS, "POST")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=True)
        store = MagicMock()
        record = self._record(key_id="kid-9", name="deploy", scopes=frozenset({"admin", "viewer"}))
        store.create = MagicMock(return_value=("raw-secret", record))
        req = self._state(
            authz=authz, principal=SimpleNamespace(subject_id="alice"), store=store, client_host="5.6.7.8"
        )
        with patch("provide.uterm.server.routes.api_keys.audit_event") as audit:
            out = await create(req, {"name": "  deploy  ", "scopes": ["admin", "viewer"], "expires_in_s": 3600})
        assert out == {
            "key": "raw-secret",
            "key_id": "kid-9",
            "name": "deploy",
            "scopes": ["admin", "viewer"],
            "created_at": 100.0,
            "expires_at": 200.0,
        }
        authz.is_admin.assert_awaited_once_with(req.state.uterm_principal)
        store.create.assert_called_once_with("deploy", scopes=frozenset({"admin", "viewer"}), expires_in_s=3600)
        audit.assert_called_once_with(
            "api_key.create",
            principal="alice",
            source_ip="5.6.7.8",
            detail={"key_id": "kid-9", "name": "deploy"},
        )

    async def test_create_expires_none_passes_through(self) -> None:
        create = _endpoint(self._router(), self._KEYS, "POST")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=True)
        store = MagicMock()
        record = self._record(expires_at=None)
        store.create = MagicMock(return_value=("raw", record))
        req = self._state(authz=authz, principal=SimpleNamespace(subject_id="bob"), store=store)
        with patch("provide.uterm.server.routes.api_keys.audit_event"):
            out = await create(req, {"name": "k", "scopes": ["admin"]})
        store.create.assert_called_once_with("k", scopes=frozenset({"admin"}), expires_in_s=None)
        assert out["expires_at"] is None

    async def test_create_not_admin_403_short_circuits(self) -> None:
        create = _endpoint(self._router(), self._KEYS, "POST")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=False)
        store = MagicMock()
        store.create = MagicMock()
        req = self._state(authz=authz, principal=object(), store=store)
        with pytest.raises(HTTPException) as exc:
            await create(req, {"name": "k", "scopes": ["admin"]})
        assert exc.value.status_code == 403
        assert exc.value.detail == "admin role required"
        store.create.assert_not_called()

    async def test_create_disabled_403_short_circuits(self) -> None:
        create = _endpoint(self._router(), self._KEYS, "POST")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=True)
        store = MagicMock()
        store.create = MagicMock()
        req = self._state(authz=authz, principal=object(), store=store, api_keys_enabled=False)
        with pytest.raises(HTTPException) as exc:
            await create(req, {"name": "k", "scopes": ["admin"]})
        assert exc.value.status_code == 403
        assert exc.value.detail == "API key management is disabled"
        store.create.assert_not_called()

    async def test_create_missing_name_422(self) -> None:
        create = _endpoint(self._router(), self._KEYS, "POST")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=True)
        store = MagicMock()
        store.create = MagicMock()
        req = self._state(authz=authz, principal=object(), store=store)
        with pytest.raises(HTTPException) as exc:
            await create(req, {"name": "   ", "scopes": ["admin"]})
        assert exc.value.status_code == 422
        assert exc.value.detail == "name is required"
        store.create.assert_not_called()

    async def test_create_scopes_key_absent_422(self) -> None:
        create = _endpoint(self._router(), self._KEYS, "POST")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=True)
        store = MagicMock()
        store.create = MagicMock()
        req = self._state(authz=authz, principal=object(), store=store)
        with pytest.raises(HTTPException) as exc:
            await create(req, {"name": "k"})
        assert exc.value.status_code == 422
        assert exc.value.detail == "scopes is required"
        store.create.assert_not_called()

    async def test_create_scopes_not_list_422(self) -> None:
        create = _endpoint(self._router(), self._KEYS, "POST")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=True)
        store = MagicMock()
        store.create = MagicMock()
        req = self._state(authz=authz, principal=object(), store=store)
        with pytest.raises(HTTPException) as exc:
            await create(req, {"name": "k", "scopes": "admin"})
        assert exc.value.status_code == 422
        assert exc.value.detail == "scopes must be a list of role scopes"
        store.create.assert_not_called()

    async def test_create_scopes_empty_after_strip_422(self) -> None:
        create = _endpoint(self._router(), self._KEYS, "POST")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=True)
        store = MagicMock()
        store.create = MagicMock()
        req = self._state(authz=authz, principal=object(), store=store)
        with pytest.raises(HTTPException) as exc:
            await create(req, {"name": "k", "scopes": ["", "   "]})
        assert exc.value.status_code == 422
        assert exc.value.detail == "scopes must include at least one role scope"
        store.create.assert_not_called()

    async def test_create_invalid_scopes_422_sorted_detail(self) -> None:
        create = _endpoint(self._router(), self._KEYS, "POST")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=True)
        store = MagicMock()
        store.create = MagicMock()
        req = self._state(authz=authz, principal=object(), store=store)
        with pytest.raises(HTTPException) as exc:
            await create(req, {"name": "k", "scopes": ["zeta", "admin", "alpha"]})
        assert exc.value.status_code == 422
        assert exc.value.detail == "invalid role scopes: alpha, zeta (allowed: admin, operator, viewer)"
        store.create.assert_not_called()

    async def test_create_expires_too_small_422(self) -> None:
        create = _endpoint(self._router(), self._KEYS, "POST")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=True)
        store = MagicMock()
        store.create = MagicMock()
        req = self._state(authz=authz, principal=object(), store=store)
        with pytest.raises(HTTPException) as exc:
            await create(req, {"name": "k", "scopes": ["admin"], "expires_in_s": 59})
        assert exc.value.status_code == 422
        assert exc.value.detail == "expires_in_s must be >= 60"
        store.create.assert_not_called()

    async def test_create_expires_boundary_60_ok(self) -> None:
        create = _endpoint(self._router(), self._KEYS, "POST")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=True)
        store = MagicMock()
        store.create = MagicMock(return_value=("raw", self._record()))
        req = self._state(authz=authz, principal=SimpleNamespace(subject_id="x"), store=store)
        with patch("provide.uterm.server.routes.api_keys.audit_event"):
            await create(req, {"name": "k", "scopes": ["admin"], "expires_in_s": 60})
        store.create.assert_called_once_with("k", scopes=frozenset({"admin"}), expires_in_s=60)

    # ---- list_api_keys ------------------------------------------------------

    async def test_list_ok_returns_rows(self) -> None:
        list_keys = _endpoint(self._router(), self._KEYS, "GET")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=True)
        store = MagicMock()
        store.list_keys = MagicMock(
            return_value=[
                self._record(
                    key_id="a",
                    name="one",
                    scopes=frozenset({"viewer", "admin"}),
                    created_at=1.0,
                    expires_at=2.0,
                    last_used_at=3.0,
                    revoked=False,
                ),
                self._record(
                    key_id="b",
                    name="two",
                    scopes=frozenset(),
                    created_at=4.0,
                    expires_at=None,
                    last_used_at=None,
                    revoked=True,
                ),
            ]
        )
        req = self._state(authz=authz, principal=object(), store=store)
        out = await list_keys(req)
        assert out == [
            {
                "key_id": "a",
                "name": "one",
                "scopes": ["admin", "viewer"],
                "created_at": 1.0,
                "expires_at": 2.0,
                "last_used_at": 3.0,
                "revoked": False,
            },
            {
                "key_id": "b",
                "name": "two",
                "scopes": [],
                "created_at": 4.0,
                "expires_at": None,
                "last_used_at": None,
                "revoked": True,
            },
        ]
        store.list_keys.assert_called_once_with()

    async def test_list_empty(self) -> None:
        list_keys = _endpoint(self._router(), self._KEYS, "GET")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=True)
        store = MagicMock()
        store.list_keys = MagicMock(return_value=[])
        req = self._state(authz=authz, principal=object(), store=store)
        assert await list_keys(req) == []

    async def test_list_not_admin_403_short_circuits(self) -> None:
        list_keys = _endpoint(self._router(), self._KEYS, "GET")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=False)
        store = MagicMock()
        store.list_keys = MagicMock()
        req = self._state(authz=authz, principal=object(), store=store)
        with pytest.raises(HTTPException) as exc:
            await list_keys(req)
        assert exc.value.status_code == 403
        assert exc.value.detail == "admin role required"
        store.list_keys.assert_not_called()

    async def test_list_disabled_403_short_circuits(self) -> None:
        list_keys = _endpoint(self._router(), self._KEYS, "GET")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=True)
        store = MagicMock()
        store.list_keys = MagicMock()
        req = self._state(authz=authz, principal=object(), store=store, api_keys_enabled=False)
        with pytest.raises(HTTPException) as exc:
            await list_keys(req)
        assert exc.value.status_code == 403
        assert exc.value.detail == "API key management is disabled"
        store.list_keys.assert_not_called()

    # ---- revoke_api_key -----------------------------------------------------

    async def test_revoke_ok_returns_and_audits(self) -> None:
        revoke = _endpoint(self._router(), self._KEY, "DELETE")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=True)
        store = MagicMock()
        store.revoke = MagicMock(return_value=True)
        req = self._state(
            authz=authz, principal=SimpleNamespace(subject_id="carol"), store=store, client_host="2.2.2.2"
        )
        with patch("provide.uterm.server.routes.api_keys.audit_event") as audit:
            out = await revoke(req, "kid-7")
        assert out == {"ok": True, "key_id": "kid-7"}
        store.revoke.assert_called_once_with("kid-7")
        audit.assert_called_once_with(
            "api_key.revoke",
            principal="carol",
            source_ip="2.2.2.2",
            detail={"key_id": "kid-7"},
        )

    async def test_revoke_unknown_key_404_no_audit(self) -> None:
        revoke = _endpoint(self._router(), self._KEY, "DELETE")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=True)
        store = MagicMock()
        store.revoke = MagicMock(return_value=False)
        req = self._state(authz=authz, principal=SimpleNamespace(subject_id="d"), store=store)
        with patch("provide.uterm.server.routes.api_keys.audit_event") as audit:
            with pytest.raises(HTTPException) as exc:
                await revoke(req, "ghost")
        assert exc.value.status_code == 404
        assert exc.value.detail == "unknown key: ghost"
        store.revoke.assert_called_once_with("ghost")
        audit.assert_not_called()

    async def test_revoke_not_admin_403_short_circuits(self) -> None:
        revoke = _endpoint(self._router(), self._KEY, "DELETE")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=False)
        store = MagicMock()
        store.revoke = MagicMock()
        req = self._state(authz=authz, principal=object(), store=store)
        with pytest.raises(HTTPException) as exc:
            await revoke(req, "kid-7")
        assert exc.value.status_code == 403
        assert exc.value.detail == "admin role required"
        store.revoke.assert_not_called()

    async def test_revoke_disabled_403_short_circuits(self) -> None:
        revoke = _endpoint(self._router(), self._KEY, "DELETE")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=True)
        store = MagicMock()
        store.revoke = MagicMock()
        req = self._state(authz=authz, principal=object(), store=store, api_keys_enabled=False)
        with pytest.raises(HTTPException) as exc:
            await revoke(req, "kid-7")
        assert exc.value.status_code == 403
        assert exc.value.detail == "API key management is disabled"
        store.revoke.assert_not_called()


# ===========================================================================
# routes/approvals.py
# ===========================================================================


class TestApprovalsRoutes:
    _LIST = "/api/approvals"
    _APPROVE = "/api/approvals/{request_id}/approve"
    _REJECT = "/api/approvals/{request_id}/reject"

    def _router(self) -> APIRouter:
        from provide.uterm.server.routes.approvals import create_approvals_router

        return create_approvals_router()

    def _approval(
        self,
        *,
        req_id: str = "req-1",
        worker_id: str = "w-1",
        submitter_id: str = "sub-1",
        command: str = "rm -rf /",
        status: str = "pending",
        created_at: float = 100.0,
        expires_at: float = 200.0,
        group_id: Any = "UNSET",
        is_fanout: Any = "UNSET",
    ) -> SimpleNamespace:
        from provide.uterm.server.bridge.hub.approvals import ApprovalStatus

        ns = SimpleNamespace(
            id=req_id,
            worker_id=worker_id,
            submitter_id=submitter_id,
            command=command,
            status=ApprovalStatus(status),
            created_at=created_at,
            expires_at=expires_at,
        )
        if group_id != "UNSET":
            ns.group_id = group_id
        if is_fanout != "UNSET":
            ns.is_fanout = is_fanout
        return ns

    def _state(self, *, requests=None, authz=None, principal=_UNSET, hub=None) -> MagicMock:
        principal = object() if principal is _UNSET else principal
        if hub is None:
            store = MagicMock(name="approval_store")
            store._requests = requests if requests is not None else {}
            hub = MagicMock(name="hub")
            hub.approval_store = store
            hub.resolve_approval = AsyncMock()
        return _request(
            app_state={"uterm_hub": hub, "uterm_authz": authz},
            state={"uterm_principal": principal},
        )

    def _admin_authz(self, *, is_admin: bool = True) -> MagicMock:
        authz = MagicMock(name="authz")
        authz.is_admin = AsyncMock(return_value=is_admin)
        return authz

    # ---- router structure ---------------------------------------------------

    def test_router_is_apirouter_with_prefix_and_tags(self) -> None:
        router = self._router()
        assert isinstance(router, APIRouter)
        assert router.prefix == "/api/approvals"
        assert router.tags == ["approvals"]

    def test_router_exposes_exact_paths_and_methods(self) -> None:
        router = self._router()
        assert _paths(router) == {self._LIST, self._APPROVE, self._REJECT}
        assert callable(_endpoint(router, self._LIST, "GET"))
        assert callable(_endpoint(router, self._APPROVE, "POST"))
        assert callable(_endpoint(router, self._REJECT, "POST"))

    # ---- list_approvals: row shape + filter ---------------------------------

    async def test_list_returns_pending_row_with_exact_shape(self) -> None:
        list_approvals = _endpoint(self._router(), self._LIST, "GET")
        row = self._approval(
            req_id="r-99",
            worker_id="w-7",
            submitter_id="alice",
            command="cat /etc/passwd",
            status="pending",
            created_at=10.5,
            expires_at=70.5,
            group_id="grp-1",
            is_fanout=True,
        )
        authz = self._admin_authz(is_admin=True)
        req = self._state(requests={"r-99": row}, authz=authz)
        out = await list_approvals(req)
        assert out == [
            {
                "id": "r-99",
                "worker_id": "w-7",
                "group_id": "grp-1",
                "is_fanout": True,
                "submitter_id": "alice",
                "command": "cat /etc/passwd",
                "status": "pending",
                "created_at": 10.5,
                "expires_at": 70.5,
            }
        ]
        authz.is_admin.assert_awaited_once_with(req.state.uterm_principal)

    async def test_list_getattr_defaults_when_fields_absent(self) -> None:
        list_approvals = _endpoint(self._router(), self._LIST, "GET")
        # group_id / is_fanout intentionally absent → getattr defaults None / False.
        row = self._approval(req_id="r-1")
        req = self._state(requests={"r-1": row}, authz=self._admin_authz())
        out = await list_approvals(req)
        assert out[0]["group_id"] is None
        assert out[0]["is_fanout"] is False

    async def test_list_excludes_non_pending_requests(self) -> None:
        list_approvals = _endpoint(self._router(), self._LIST, "GET")
        pending = self._approval(req_id="p", status="pending")
        approved = self._approval(req_id="a", status="approved")
        rejected = self._approval(req_id="x", status="rejected")
        req = self._state(
            requests={"p": pending, "a": approved, "x": rejected},
            authz=self._admin_authz(),
        )
        out = await list_approvals(req)
        assert [r["id"] for r in out] == ["p"]
        assert out[0]["status"] == "pending"

    async def test_list_empty_when_no_requests(self) -> None:
        list_approvals = _endpoint(self._router(), self._LIST, "GET")
        req = self._state(requests={}, authz=self._admin_authz())
        assert await list_approvals(req) == []

    async def test_list_requires_principal_401(self) -> None:
        list_approvals = _endpoint(self._router(), self._LIST, "GET")
        authz = self._admin_authz()
        req = self._state(requests={}, authz=authz, principal=None)
        with pytest.raises(HTTPException) as exc:
            await list_approvals(req)
        assert exc.value.status_code == 401
        assert exc.value.detail == "Authentication required"
        authz.is_admin.assert_not_awaited()

    async def test_list_requires_admin_403(self) -> None:
        list_approvals = _endpoint(self._router(), self._LIST, "GET")
        store = MagicMock()
        store._requests = {"r": self._approval()}
        authz = self._admin_authz(is_admin=False)
        req = _request(
            app_state={"uterm_hub": MagicMock(_approval_store=store), "uterm_authz": authz},
            state={"uterm_principal": object()},
        )
        with pytest.raises(HTTPException) as exc:
            await list_approvals(req)
        assert exc.value.status_code == 403
        assert exc.value.detail == "Admin role required"

    # ---- approve_command ----------------------------------------------------

    async def test_approve_ok_resolves_and_returns_status(self) -> None:
        from provide.uterm.server.bridge.hub.approvals import ApprovalStatus
        from provide.uterm.server.bridge.hub.ext import PolicyDecision

        approve = _endpoint(self._router(), self._APPROVE, "POST")
        row = self._approval(req_id="r-5", worker_id="w-22", command="halt")
        store = MagicMock()
        store.get = MagicMock(return_value=row)
        store.claim = MagicMock(return_value=True)
        hub = MagicMock()
        hub.approval_store = store
        hub.resolve_approval = AsyncMock()
        authz = self._admin_authz()
        req = _request(
            app_state={"uterm_hub": hub, "uterm_authz": authz},
            state={"uterm_principal": object()},
        )
        out = await approve("r-5", req)
        assert out == {"status": "approved"}
        store.get.assert_called_once_with("r-5")
        store.claim.assert_called_once_with("r-5", ApprovalStatus.APPROVED)
        hub.resolve_approval.assert_awaited_once_with("w-22", "r-5", PolicyDecision(action="allow"), "halt")

    async def test_approve_not_found_404(self) -> None:
        approve = _endpoint(self._router(), self._APPROVE, "POST")
        store = MagicMock()
        store.get = MagicMock(return_value=None)
        store.claim = MagicMock()
        hub = MagicMock()
        hub.approval_store = store
        hub.resolve_approval = AsyncMock()
        req = _request(
            app_state={"uterm_hub": hub, "uterm_authz": self._admin_authz()},
            state={"uterm_principal": object()},
        )
        with pytest.raises(HTTPException) as exc:
            await approve("ghost", req)
        assert exc.value.status_code == 404
        assert exc.value.detail == "Approval request not found"
        store.claim.assert_not_called()
        hub.resolve_approval.assert_not_awaited()

    async def test_approve_not_pending_400(self) -> None:
        approve = _endpoint(self._router(), self._APPROVE, "POST")
        store = MagicMock()
        store.get = MagicMock(return_value=self._approval(req_id="r-5"))
        store.claim = MagicMock(return_value=False)
        hub = MagicMock()
        hub.approval_store = store
        hub.resolve_approval = AsyncMock()
        req = _request(
            app_state={"uterm_hub": hub, "uterm_authz": self._admin_authz()},
            state={"uterm_principal": object()},
        )
        with pytest.raises(HTTPException) as exc:
            await approve("r-5", req)
        assert exc.value.status_code == 400
        assert exc.value.detail == "Approval request is not pending"
        hub.resolve_approval.assert_not_awaited()

    async def test_approve_requires_admin_403_short_circuits(self) -> None:
        approve = _endpoint(self._router(), self._APPROVE, "POST")
        store = MagicMock()
        store.get = MagicMock()
        store.claim = MagicMock()
        hub = MagicMock()
        hub.approval_store = store
        hub.resolve_approval = AsyncMock()
        req = _request(
            app_state={"uterm_hub": hub, "uterm_authz": self._admin_authz(is_admin=False)},
            state={"uterm_principal": object()},
        )
        with pytest.raises(HTTPException) as exc:
            await approve("r-5", req)
        assert exc.value.status_code == 403
        assert exc.value.detail == "Admin role required"
        store.get.assert_not_called()
        store.claim.assert_not_called()
        hub.resolve_approval.assert_not_awaited()

    # ---- reject_command -----------------------------------------------------

    async def test_reject_ok_with_reason_resolves_and_returns_status(self) -> None:
        from provide.uterm.server.bridge.hub.approvals import ApprovalStatus
        from provide.uterm.server.bridge.hub.ext import PolicyDecision

        reject = _endpoint(self._router(), self._REJECT, "POST")
        row = self._approval(req_id="r-8", worker_id="w-9", command="shutdown")
        store = MagicMock()
        store.get = MagicMock(return_value=row)
        store.claim = MagicMock(return_value=True)
        hub = MagicMock()
        hub.approval_store = store
        hub.resolve_approval = AsyncMock()
        req = _request(
            app_state={"uterm_hub": hub, "uterm_authz": self._admin_authz()},
            state={"uterm_principal": object()},
        )
        out = await reject("r-8", req, "too risky")
        assert out == {"status": "rejected"}
        store.get.assert_called_once_with("r-8")
        store.claim.assert_called_once_with("r-8", ApprovalStatus.REJECTED)
        hub.resolve_approval.assert_awaited_once_with(
            "w-9", "r-8", PolicyDecision(action="deny", reason="too risky"), "shutdown"
        )

    async def test_reject_default_reason_is_none(self) -> None:
        from provide.uterm.server.bridge.hub.ext import PolicyDecision

        reject = _endpoint(self._router(), self._REJECT, "POST")
        row = self._approval(req_id="r-8", worker_id="w-9", command="shutdown")
        store = MagicMock()
        store.get = MagicMock(return_value=row)
        store.claim = MagicMock(return_value=True)
        hub = MagicMock()
        hub.approval_store = store
        hub.resolve_approval = AsyncMock()
        req = _request(
            app_state={"uterm_hub": hub, "uterm_authz": self._admin_authz()},
            state={"uterm_principal": object()},
        )
        out = await reject("r-8", req)
        assert out == {"status": "rejected"}
        hub.resolve_approval.assert_awaited_once_with(
            "w-9", "r-8", PolicyDecision(action="deny", reason=None), "shutdown"
        )

    async def test_reject_not_found_404(self) -> None:
        reject = _endpoint(self._router(), self._REJECT, "POST")
        store = MagicMock()
        store.get = MagicMock(return_value=None)
        store.claim = MagicMock()
        hub = MagicMock()
        hub.approval_store = store
        hub.resolve_approval = AsyncMock()
        req = _request(
            app_state={"uterm_hub": hub, "uterm_authz": self._admin_authz()},
            state={"uterm_principal": object()},
        )
        with pytest.raises(HTTPException) as exc:
            await reject("ghost", req)
        assert exc.value.status_code == 404
        assert exc.value.detail == "Approval request not found"
        store.claim.assert_not_called()
        hub.resolve_approval.assert_not_awaited()

    async def test_reject_not_pending_400(self) -> None:
        reject = _endpoint(self._router(), self._REJECT, "POST")
        store = MagicMock()
        store.get = MagicMock(return_value=self._approval(req_id="r-8"))
        store.claim = MagicMock(return_value=False)
        hub = MagicMock()
        hub.approval_store = store
        hub.resolve_approval = AsyncMock()
        req = _request(
            app_state={"uterm_hub": hub, "uterm_authz": self._admin_authz()},
            state={"uterm_principal": object()},
        )
        with pytest.raises(HTTPException) as exc:
            await reject("r-8", req)
        assert exc.value.status_code == 400
        assert exc.value.detail == "Approval request is not pending"
        hub.resolve_approval.assert_not_awaited()

    async def test_reject_requires_principal_401_short_circuits(self) -> None:
        reject = _endpoint(self._router(), self._REJECT, "POST")
        store = MagicMock()
        store.get = MagicMock()
        store.claim = MagicMock()
        hub = MagicMock()
        hub.approval_store = store
        hub.resolve_approval = AsyncMock()
        authz = self._admin_authz()
        req = _request(
            app_state={"uterm_hub": hub, "uterm_authz": authz},
            state={"uterm_principal": None},
        )
        with pytest.raises(HTTPException) as exc:
            await reject("r-8", req)
        assert exc.value.status_code == 401
        assert exc.value.detail == "Authentication required"
        authz.is_admin.assert_not_awaited()
        store.get.assert_not_called()
        hub.resolve_approval.assert_not_awaited()


# ===========================================================================
# sse.py / api.py / sessions.py / tunnels.py
# ===========================================================================


class TestSseRoutes:
    def _router(self) -> APIRouter:
        from provide.uterm.server.routes.sse import create_sse_router

        return create_sse_router()

    _PATH = "/sessions/{session_id}/events/stream"

    def test_registry_authz_accessors(self) -> None:
        from provide.uterm.server.routes.sse import _authz, _registry

        reg, az = object(), object()
        assert _registry(_request(app_state={"uterm_registry": reg})) is reg
        assert _authz(_request(app_state={"uterm_authz": az})) is az

    def test_router_has_stream_path(self) -> None:
        router = self._router()
        assert isinstance(router, APIRouter)
        assert self._PATH in _paths(router)

    async def test_stream_unknown_session_404(self) -> None:
        ep = _endpoint(self._router(), self._PATH)
        reg = MagicMock()
        reg.get_definition = AsyncMock(return_value=None)
        req = _request(
            app_state={"uterm_registry": reg, "uterm_authz": MagicMock()}, state={"uterm_principal": object()}
        )
        with pytest.raises(HTTPException) as exc:
            await ep(req, "ghost")
        assert exc.value.status_code == 404
        assert exc.value.detail == "unknown session: ghost"

    async def test_stream_forbidden_403(self) -> None:
        ep = _endpoint(self._router(), self._PATH)
        reg = MagicMock()
        reg.get_definition = AsyncMock(return_value=SimpleNamespace())
        az = MagicMock()
        az.can_read_session = AsyncMock(return_value=False)
        req = _request(app_state={"uterm_registry": reg, "uterm_authz": az}, state={"uterm_principal": object()})
        with pytest.raises(HTTPException) as exc:
            await ep(req, "s1")
        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"

    async def test_stream_ok_passes_heartbeat_and_parsed_event_types(self) -> None:
        from fastapi.responses import StreamingResponse

        ep = _endpoint(self._router(), self._PATH)
        reg = MagicMock()
        reg.get_definition = AsyncMock(return_value=SimpleNamespace())

        async def _gen() -> Any:  # pragma: no cover - never iterated here
            yield "data: x\n\n"

        reg.stream_session_events = MagicMock(return_value=_gen())
        az = MagicMock()
        az.can_read_session = AsyncMock(return_value=True)
        req = _request(app_state={"uterm_registry": reg, "uterm_authz": az}, state={"uterm_principal": object()})
        resp = await ep(req, "s1", event_types="a, b ,,c", pattern="p")
        assert isinstance(resp, StreamingResponse)
        assert resp.media_type == "text/event-stream"
        reg.stream_session_events.assert_called_once_with(
            "s1", event_types=["a", "b", "c"], pattern="p", heartbeat_s=15.0
        )

    async def test_stream_no_event_types_passes_none(self) -> None:
        ep = _endpoint(self._router(), self._PATH)
        reg = MagicMock()
        reg.get_definition = AsyncMock(return_value=SimpleNamespace())

        async def _gen() -> Any:  # pragma: no cover
            yield "x"

        reg.stream_session_events = MagicMock(return_value=_gen())
        az = MagicMock()
        az.can_read_session = AsyncMock(return_value=True)
        req = _request(app_state={"uterm_registry": reg, "uterm_authz": az}, state={"uterm_principal": object()})
        await ep(req, "s1")
        assert reg.stream_session_events.call_args.kwargs["event_types"] is None


class TestApiRouter:
    def _router(self) -> APIRouter:
        from provide.uterm.server.routes.api import create_api_router

        return create_api_router()

    def test_prefix_and_subrouters_aggregated(self) -> None:
        router = self._router()
        assert isinstance(router, APIRouter)
        paths = _paths(router)
        # metrics endpoints + at least one path from each included sub-router, all under /api
        assert "/api/metrics" in paths
        assert "/api/metrics/prometheus" in paths
        assert "/api/sessions/{session_id}/events/stream" in paths  # sse sub-router, prefixed
        assert all(p.startswith("/api") for p in paths if p)

    async def test_metrics_dict_passthrough_and_non_dict_default(self) -> None:
        metrics = _endpoint(self._router(), "/api/metrics")
        assert await metrics(_request(app_state={"uterm_metrics": {"a": 1}})) == {"metrics": {"a": 1}}
        assert await metrics(_request(app_state={"uterm_metrics": "not-a-dict"})) == {"metrics": {}}

    async def test_metrics_prometheus_formats_sorted_and_empty(self) -> None:
        from fastapi.responses import PlainTextResponse

        prom = _endpoint(self._router(), "/api/metrics/prometheus")
        resp = await prom(_request(app_state={"uterm_metrics": {"b": 2, "a": 1}}))
        assert isinstance(resp, PlainTextResponse)
        assert resp.body.decode() == "# TYPE a counter\na 1\n# TYPE b counter\nb 2\n"
        empty = await prom(_request(app_state={"uterm_metrics": {}}))
        assert empty.body.decode() == ""

    async def test_metrics_open_when_require_auth_false(self) -> None:
        cfg = SimpleNamespace(security=SimpleNamespace(metrics_require_auth=False), auth=SimpleNamespace())
        metrics = _endpoint(self._router(), "/api/metrics")
        out = await metrics(_request(app_state={"uterm_metrics": {"a": 1}, "uterm_config": cfg}))
        assert out == {"metrics": {"a": 1}}

    async def test_metrics_require_auth_rejects_anonymous(self) -> None:
        cfg = SimpleNamespace(security=SimpleNamespace(metrics_require_auth=True), auth=SimpleNamespace())
        req = _request(app_state={"uterm_metrics": {"a": 1}, "uterm_config": cfg})
        metrics = _endpoint(self._router(), "/api/metrics")
        with (
            patch(
                "provide.uterm.server.auth.resolve_http_principal",
                return_value=SimpleNamespace(subject_id="anonymous"),
            ) as resolve,
            pytest.raises(HTTPException) as exc,
        ):
            await metrics(req)
        assert exc.value.status_code == 401
        # Pin the exact detail string — kills the detail=None / dropped /
        # "XX..XX" / case-folded mutants in the HTTPException.
        assert exc.value.detail == "authentication required for /metrics"
        # Pin the resolver call args — kills the request->None, cfg.auth->None,
        # and dropped-argument mutants in the resolve_http_principal(...) call.
        resolve.assert_called_once_with(req, cfg.auth)

    async def test_metrics_require_auth_allows_authenticated(self) -> None:
        cfg = SimpleNamespace(security=SimpleNamespace(metrics_require_auth=True), auth=SimpleNamespace())
        req = _request(app_state={"uterm_metrics": {"a": 1}, "uterm_config": cfg})
        metrics = _endpoint(self._router(), "/api/metrics")
        with patch(
            "provide.uterm.server.auth.resolve_http_principal",
            return_value=SimpleNamespace(subject_id="alice"),
        ) as resolve:
            out = await metrics(req)
        assert out == {"metrics": {"a": 1}}
        resolve.assert_called_once_with(req, cfg.auth)

    async def test_metrics_prometheus_require_auth_rejects_anonymous(self) -> None:
        cfg = SimpleNamespace(security=SimpleNamespace(metrics_require_auth=True), auth=SimpleNamespace())
        req = _request(app_state={"uterm_metrics": {"a": 1}, "uterm_config": cfg})
        prom = _endpoint(self._router(), "/api/metrics/prometheus")
        with (
            patch(
                "provide.uterm.server.auth.resolve_http_principal",
                return_value=SimpleNamespace(subject_id="anonymous"),
            ) as resolve,
            pytest.raises(HTTPException) as exc,
        ):
            await prom(req)
        assert exc.value.status_code == 401
        assert exc.value.detail == "authentication required for /metrics"
        resolve.assert_called_once_with(req, cfg.auth)


class TestSessionsRouter:
    def test_returns_router_with_routes(self) -> None:
        from provide.uterm.server.routes.sessions import create_sessions_router

        router = create_sessions_router()  # mutant `router = None` → AttributeError on @router.get
        assert isinstance(router, APIRouter)
        assert "/sessions" in _paths(router)


class TestTunnelsRouter:
    def test_returns_router_with_routes(self) -> None:
        from provide.uterm.server.routes.tunnels import create_tunnels_router

        router = create_tunnels_router()  # mutant `router = None` → AttributeError
        assert isinstance(router, APIRouter)
        assert "/connect" in _paths(router)

    def test_scrub_sensitive_masks_only_sensitive_keys(self) -> None:
        from provide.uterm.server.routes.tunnels import _scrub_sensitive

        out = _scrub_sensitive(
            {"password": "p", "passphrase": "x", "secret": "s", "token": "t", "host": "h", "port": 22}
        )
        assert out == {"password": "***", "passphrase": "***", "secret": "***", "token": "***", "host": "h", "port": 22}


# ===========================================================================
# Targeted kills: getattr-drop-default (absent attr) + health branch/arg
# ===========================================================================


class TestGetattrAndBranchKills:
    """An ABSENT request attribute distinguishes getattr(x, k, None) from getattr(x, k):
    the original returns None (then raises the accessor's HTTPException), the
    drop-default mutant raises AttributeError. We assert the HTTPException so the
    mutant's AttributeError fails the test = a kill."""

    def test_helpers_principal_absent_attr_500(self) -> None:
        from provide.uterm.server.routes._helpers import principal

        with pytest.raises(HTTPException) as exc:
            principal(_request())  # state has NO uterm_principal attribute
        assert exc.value.status_code == 500

    def test_api_keys_principal_absent_attr_500(self) -> None:
        from provide.uterm.server.routes.api_keys import _principal

        with pytest.raises(HTTPException) as exc:
            _principal(_request())
        assert exc.value.status_code == 500

    def test_webhooks_principal_absent_attr_500(self) -> None:
        from provide.uterm.server.routes.webhooks import _principal

        with pytest.raises(HTTPException) as exc:
            _principal(_request())
        assert exc.value.status_code == 500

    def test_webhooks_manager_absent_attr_503(self) -> None:
        from provide.uterm.server.routes.webhooks import _webhook_manager

        with pytest.raises(HTTPException) as exc:
            _webhook_manager(_request())  # app.state has NO uterm_webhooks attribute
        assert exc.value.status_code == 503

    def test_profiles_principal_absent_attr_500(self) -> None:
        from provide.uterm.server.routes.profiles import _principal

        with pytest.raises(HTTPException) as exc:
            _principal(_request())
        assert exc.value.status_code == 500

    async def test_approvals_list_absent_principal_401(self) -> None:
        from provide.uterm.server.routes.approvals import create_approvals_router

        router = create_approvals_router()
        ep = _endpoint(router, "/api/approvals", "GET")
        req = _request(app_state={"uterm_hub": MagicMock()})  # NO uterm_principal in state
        with pytest.raises(HTTPException) as exc:
            await ep(req)
        assert exc.value.status_code == 401

    # --- health _posture_caller_is_privileged getattr-drop + branch/arg ---

    def _posture_router(self):  # type: ignore[no-untyped-def]
        from provide.uterm.server.routes.health import create_health_router

        return create_health_router(require_authenticated=AsyncMock())

    _POSTURE = {"environment": "prod", "secure": True, "dev_opt_outs": ["x"], "warnings": ["w"]}

    async def test_posture_absent_principal_is_coarse(self) -> None:
        from unittest.mock import patch

        sp = _endpoint(self._posture_router(), "/api/security-posture")
        req = _request(app_state={"uterm_config": object(), "uterm_authz": MagicMock()})  # NO uterm_principal
        with patch("provide.uterm.server.routes.health.compute_security_posture", return_value=self._POSTURE):
            out = await sp(req)
        assert out == {"environment": "prod", "secure": True}

    async def test_posture_absent_authz_is_coarse(self) -> None:
        from unittest.mock import patch

        sp = _endpoint(self._posture_router(), "/api/security-posture")
        # principal present, but app.state has NO uterm_authz attribute
        req = _request(app_state={"uterm_config": object()}, state={"uterm_principal": object()})
        with patch("provide.uterm.server.routes.health.compute_security_posture", return_value=self._POSTURE):
            out = await sp(req)
        assert out == {"environment": "prod", "secure": True}

    async def test_posture_none_principal_present_authz_admin_is_full(self) -> None:
        from unittest.mock import patch

        # principal None (present) + authz present whose is_admin(None) → True. Original short-circuits
        # to coarse via `principal is None or ...`; the `and` mutant proceeds to is_admin → full.
        sp = _endpoint(self._posture_router(), "/api/security-posture")
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=True)
        authz.has_role = AsyncMock(return_value=False)
        req = _request(app_state={"uterm_config": object(), "uterm_authz": authz}, state={"uterm_principal": None})
        with patch("provide.uterm.server.routes.health.compute_security_posture", return_value=self._POSTURE):
            out = await sp(req)
        assert out == {"environment": "prod", "secure": True}  # coarse — `and` mutant would return full

    async def test_posture_is_admin_called_with_real_principal(self) -> None:
        from unittest.mock import patch

        sp = _endpoint(self._posture_router(), "/api/security-posture")
        principal = object()
        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=True)
        authz.has_role = AsyncMock(return_value=False)
        req = _request(app_state={"uterm_config": object(), "uterm_authz": authz}, state={"uterm_principal": principal})
        with patch("provide.uterm.server.routes.health.compute_security_posture", return_value=self._POSTURE):
            out = await sp(req)
        assert out == self._POSTURE
        authz.is_admin.assert_awaited_once_with(principal)  # kills is_admin(None)
