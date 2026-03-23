#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for HTML page routes (session, operator, replay, connect, dashboard)."""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi.testclient import TestClient

from provide.terminal.server import create_server_app, default_server_config
from provide.terminal.server.models import AuthConfig, SessionDefinition

_TEST_KEY = "uterm-test-secret-32-byte-minimum-key"


def _make_token(sub: str = "user1", roles: list[str] | None = None) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": sub,
            "roles": roles or ["viewer"],
            "iss": "provide-terminal",
            "aud": "provide-terminal-server",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        key=_TEST_KEY,
        algorithm="HS256",
    )


def _jwt_app_with_operator_session(session_id: str):  # type: ignore[return]
    cfg = default_server_config()
    cfg.auth = AuthConfig(
        mode="jwt",
        jwt_public_key_pem=_TEST_KEY,
        jwt_algorithms=["HS256"],
        jwt_issuer="provide-terminal",
        jwt_audience="provide-terminal-server",
        worker_bearer_token=_make_token(sub="worker", roles=["admin"]),
    )
    cfg.sessions = [
        SessionDefinition(
            session_id=session_id,
            display_name="Operator Only",
            connector_type="shell",
            visibility="operator",
        )
    ]
    return create_server_app(cfg)


def _make_app_with_session(visibility: str = "public"):  # type: ignore[return]
    cfg = default_server_config()
    cfg.auth.mode = "dev"
    cfg.sessions = [
        SessionDefinition(
            session_id="test-sess",
            display_name="Test Session",
            connector_type="shell",
            visibility=visibility,  # type: ignore[arg-type]
        )
    ]
    return create_server_app(cfg)


@pytest.fixture()
def client() -> TestClient:
    with TestClient(_make_app_with_session()) as c:
        yield c  # type: ignore[misc]


# ── dashboard ────────────────────────────────────────────────────────────────


def test_dashboard_html_includes_fitaddon_cdn(client: TestClient) -> None:
    r = client.get("/app/")
    assert r.status_code == 200
    assert "addon-fit.js" in r.text
    assert "xterm.js" in r.text
    assert "xterm.css" in r.text


# ── session_view ─────────────────────────────────────────────────────────────


def test_session_view_404_unknown(client: TestClient) -> None:
    r = client.get("/app/session/no-such-session")
    assert r.status_code == 404


def test_session_view_403_insufficient_privileges() -> None:
    app = _jwt_app_with_operator_session("priv-sess")
    headers = {"Authorization": f"Bearer {_make_token(sub='viewer', roles=['viewer'])}"}
    with TestClient(app, headers=headers) as c:
        r = c.get("/app/session/priv-sess")
    assert r.status_code == 403


def test_session_view_200_sets_cookies_and_fitaddon(client: TestClient) -> None:
    r = client.get("/app/session/test-sess")
    assert r.status_code == 200
    assert "addon-fit.js" in r.text
    assert "xterm.js" in r.text
    assert '"page_kind": "session"' in r.text
    cookies = ",".join(r.headers.get_list("set-cookie"))
    assert "uterm_surface=user" in cookies


# ── operator_session ─────────────────────────────────────────────────────────


def test_operator_session_404_unknown(client: TestClient) -> None:
    r = client.get("/app/operator/no-such-session")
    assert r.status_code == 404


def test_operator_session_403_insufficient_privileges() -> None:
    app = _jwt_app_with_operator_session("priv-op")
    headers = {"Authorization": f"Bearer {_make_token(sub='viewer', roles=['viewer'])}"}
    with TestClient(app, headers=headers) as c:
        r = c.get("/app/operator/priv-op")
    assert r.status_code == 403


def test_operator_session_200_sets_cookies_and_fitaddon(client: TestClient) -> None:
    r = client.get("/app/operator/test-sess")
    assert r.status_code == 200
    assert "addon-fit.js" in r.text
    assert "xterm.js" in r.text
    assert '"page_kind": "operator"' in r.text
    cookies = ",".join(r.headers.get_list("set-cookie"))
    assert "uterm_surface=operator" in cookies


# ── replay_view ──────────────────────────────────────────────────────────────


def test_replay_view_404_unknown(client: TestClient) -> None:
    r = client.get("/app/replay/no-such-session")
    assert r.status_code == 404


def test_replay_view_403_insufficient_privileges() -> None:
    app = _jwt_app_with_operator_session("priv-rep")
    headers = {"Authorization": f"Bearer {_make_token(sub='viewer', roles=['viewer'])}"}
    with TestClient(app, headers=headers) as c:
        r = c.get("/app/replay/priv-rep")
    assert r.status_code == 403


def test_replay_view_200_sets_cookies_and_fitaddon(client: TestClient) -> None:
    r = client.get("/app/replay/test-sess")
    assert r.status_code == 200
    assert "addon-fit.js" in r.text
    assert "xterm.js" in r.text
    assert '"page_kind": "replay"' in r.text
    cookies = ",".join(r.headers.get_list("set-cookie"))
    assert "uterm_surface=operator" in cookies


# ── connect_view ─────────────────────────────────────────────────────────────


def test_connect_view_200_includes_fitaddon(client: TestClient) -> None:
    r = client.get("/app/connect")
    assert r.status_code == 200
    assert "addon-fit.js" in r.text
    assert '"page_kind": "connect"' in r.text


# ── token_cookie on page requests ────────────────────────────────────────────


def _jwt_app_public_session(session_id: str):  # type: ignore[return]
    cfg = default_server_config()
    cfg.auth = AuthConfig(
        mode="jwt",
        jwt_public_key_pem=_TEST_KEY,
        jwt_algorithms=["HS256"],
        jwt_issuer="provide-terminal",
        jwt_audience="provide-terminal-server",
        worker_bearer_token=_make_token(sub="worker", roles=["admin"]),
    )
    cfg.sessions = [
        SessionDefinition(
            session_id=session_id,
            display_name="Public Session",
            connector_type="shell",
            visibility="public",
        )
    ]
    return create_server_app(cfg)


def test_jwt_page_sets_token_cookie_when_bearer_present() -> None:
    """JWT mode + Bearer header on page request → response has token_cookie set."""
    token = _make_token(sub="user1", roles=["admin"])
    app = _jwt_app_public_session("tok-sess")
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as c:
        r = c.get("/app/session/tok-sess")
    assert r.status_code == 200
    set_cookies = ",".join(r.headers.get_list("set-cookie"))
    assert "uterm_token=" in set_cookies


def test_dev_mode_page_does_not_set_token_cookie() -> None:
    """Dev mode + Bearer header on page request → token_cookie NOT set."""
    token = _make_token(sub="user1", roles=["admin"])
    app = _make_app_with_session("public")
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as c:
        r = c.get("/app/session/test-sess")
    assert r.status_code == 200
    set_cookies = ",".join(r.headers.get_list("set-cookie"))
    assert "uterm_token=" not in set_cookies


def test_jwt_page_no_bearer_does_not_set_token_cookie() -> None:
    """JWT mode + no Bearer header on page request → token_cookie NOT set."""
    token = _make_token(sub="user1", roles=["admin"])
    app = _jwt_app_public_session("tok-sess2")
    # Seed the token_cookie directly so the page auth succeeds without Bearer
    with TestClient(app) as c:
        c.cookies.set("uterm_token", token)
        r = c.get("/app/session/tok-sess2")
    assert r.status_code == 200
    set_cookies = ",".join(r.headers.get_list("set-cookie"))
    assert "uterm_token=" not in set_cookies


def test_jwt_page_sets_principal_cookie_value() -> None:
    """principal_cookie must carry the actual subject, not None."""
    token = _make_token(sub="alice", roles=["admin"])
    app = _jwt_app_public_session("pcv-sess")
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as c:
        r = c.get("/app/")
    assert r.status_code == 200
    set_cookies = ",".join(r.headers.get_list("set-cookie"))
    assert "uterm_principal=alice" in set_cookies


def test_page_cookies_have_secure_flag_over_https() -> None:
    """Cookies must carry the Secure attribute when X-Forwarded-Proto: https."""
    token = _make_token(sub="user1", roles=["admin"])
    app = _jwt_app_public_session("https-sess")
    with TestClient(app) as c:
        # X-Forwarded-Proto: https triggers _is_secure_request → secure=True
        r = c.get(
            "/app/",
            headers={"Authorization": f"Bearer {token}", "X-Forwarded-Proto": "https"},
        )
    assert r.status_code == 200
    # Every Set-Cookie header must have the Secure attribute
    for cookie_header in r.headers.get_list("set-cookie"):
        assert "Secure" in cookie_header, f"missing Secure on: {cookie_header}"


def test_token_cookie_has_secure_flag_over_https() -> None:
    """token_cookie must also carry Secure when the request is over HTTPS."""
    token = _make_token(sub="user1", roles=["admin"])
    app = _jwt_app_public_session("https-tok-sess")
    with TestClient(app) as c:
        r = c.get(
            "/app/session/https-tok-sess",
            headers={"Authorization": f"Bearer {token}", "X-Forwarded-Proto": "https"},
        )
    assert r.status_code == 200
    token_cookie_headers = [h for h in r.headers.get_list("set-cookie") if "uterm_token=" in h]
    assert token_cookie_headers, "uterm_token cookie not set"
    assert "Secure" in token_cookie_headers[0]


def test_set_page_cookies_skips_token_for_anonymous() -> None:
    """_set_page_cookies must not write token_cookie when principal is anonymous."""
    from fastapi.responses import HTMLResponse
    from starlette.requests import Request

    from provide.terminal.server.routes.pages import _set_page_cookies

    cfg = default_server_config()
    cfg.auth = AuthConfig(
        mode="jwt",
        jwt_public_key_pem=_TEST_KEY,
        jwt_algorithms=["HS256"],
        jwt_issuer="provide-terminal",
        jwt_audience="provide-terminal-server",
        worker_bearer_token=_make_token(sub="worker", roles=["admin"]),
    )
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [
            (b"authorization", f"Bearer {_make_token(sub='x')}".encode()),
        ],
        "query_string": b"",
    }
    req = Request(scope)
    resp = HTMLResponse("ok")
    # Call with principal_name="anonymous" — must NOT set token_cookie
    _set_page_cookies(resp, req, cfg, "anonymous", "operator", secure=False)
    set_cookies = b",".join(v for k, v in resp.raw_headers if k == b"set-cookie")
    assert b"uterm_token=" not in set_cookies.lower()
    assert b"uterm_principal=anonymous" in set_cookies


def test_jwt_token_cookie_enables_subsequent_api_call() -> None:
    """Full browser flow: page request → token_cookie → API call without Bearer → 200."""
    token = _make_token(sub="user1", roles=["admin"])
    app = _jwt_app_public_session("flow-sess")
    with TestClient(app) as c:
        # Step 1: page request with Bearer header
        r1 = c.get("/app/", headers={"Authorization": f"Bearer {token}"})
        assert r1.status_code == 200
        # token_cookie must be set
        set_cookies = ",".join(r1.headers.get_list("set-cookie"))
        assert "uterm_token=" in set_cookies
        # TestClient automatically stores cookies from Set-Cookie headers
        # Step 2: API call without Authorization header
        r2 = c.get("/api/sessions")
        assert r2.status_code == 200
