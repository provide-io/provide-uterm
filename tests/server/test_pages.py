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
