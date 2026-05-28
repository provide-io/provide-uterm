#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Additional auth/factory branch coverage tests for create_server_app."""

from __future__ import annotations

import os
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import HTTPConnection

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.auth import Principal
from provide.uterm.server.models import RecordingConfig, TunnelConfig


def _make_app(**config_overrides: Any) -> tuple[Any, Any]:
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    config.sessions = []
    for key, value in config_overrides.items():
        setattr(config, key, value)
    return create_server_app(config), config


def _auth_dep(app: Any) -> Any:
    from fastapi.routing import APIRoute

    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == "/api/sessions":
            dependency = route.dependencies[0]
            return dependency.dependency
    raise AssertionError("auth dependency not found")


async def _run_lifespan_one_tick(app: Any) -> None:
    import asyncio

    ran_once: set[int] = set()
    _real_sleep = asyncio.sleep

    async def _patched_sleep(_delay: float) -> None:
        task = id(asyncio.current_task())
        if task in ran_once:
            await _real_sleep(3600)
            return
        ran_once.add(task)
        await _real_sleep(0)

    with patch("asyncio.sleep", _patched_sleep):
        async with app.router.lifespan_context(app):
            for _ in range(20):
                await _real_sleep(0)


class TestAuthDependencyBranches:
    async def test_share_cookie_transport_sets_viewer_principal(self) -> None:
        app, _ = _make_app(tunnel=TunnelConfig(token_transport="cookie"))
        app.state.uterm_tunnel_tokens["sess1"] = {"share_token_hash": "x", "control_token_hash": "y"}
        dep = _auth_dep(app)
        conn = HTTPConnection(
            {
                "type": "http",
                "path": "/api/sessions/sess1",
                "headers": [(b"cookie", b"uterm_tunnel_sess1=tok")],
                "query_string": b"",
                "app": app,
                "client": ("testclient", 1234),
                "state": {},
            }
        )
        with patch("provide.uterm.tunnel.token_hash.verify_token", side_effect=[False, True]):
            await dep(conn)
        assert conn.state.uterm_principal.subject_id == "share:sess1:viewer"

    async def test_share_control_cookie_sets_operator_principal(self) -> None:
        app, _ = _make_app(tunnel=TunnelConfig(token_transport="cookie"))
        app.state.uterm_tunnel_tokens["sess2"] = {"share_token_hash": "x", "control_token_hash": "y"}
        dep = _auth_dep(app)
        conn = HTTPConnection(
            {
                "type": "http",
                "path": "/api/sessions/sess2",
                "headers": [(b"cookie", b"uterm_tunnel_sess2=tok")],
                "query_string": b"",
                "app": app,
                "client": ("testclient", 1234),
                "state": {},
            }
        )
        with patch("provide.uterm.tunnel.token_hash.verify_token", side_effect=[True]):
            await dep(conn)
        assert conn.state.uterm_principal.subject_id == "share:sess2:operator"

    async def test_tunnel_ws_worker_token_validates(self) -> None:
        app, _ = _make_app()
        app.state.uterm_tunnel_tokens["w1"] = {
            "worker_token_hash": "h",
            "expires_at": time.time() + 3600,
            "issued_ip": "testclient",
        }
        dep = _auth_dep(app)
        conn = HTTPConnection(
            {
                "type": "websocket",
                "path": "/tunnel/w1",
                "headers": [(b"authorization", b"Bearer tok")],
                "query_string": b"",
                "app": app,
                "client": ("testclient", 1234),
                "state": {},
            }
        )
        with patch("provide.uterm.tunnel.token_hash.verify_token", return_value=True):
            await dep(conn)
        assert conn.state.uterm_principal.subject_id == "worker"

    async def test_share_cookie_ip_binding_mismatch_falls_back_to_http_auth(self) -> None:
        from provide.uterm.tunnel.token_hash import hash_token

        app, _ = _make_app(tunnel=TunnelConfig(ip_binding=True, token_transport="cookie"))
        app.state.uterm_tunnel_tokens["sess-ipbad"] = {
            "share_token_hash": hash_token("tok"),
            "control_token_hash": hash_token("control"),
            "expires_at": time.time() + 3600,
            "issued_ip": "10.10.10.10",
        }
        dep = _auth_dep(app)
        conn = HTTPConnection(
            {
                "type": "http",
                "path": "/api/sessions/sess-ipbad",
                "headers": [(b"cookie", b"uterm_tunnel_sess-ipbad=tok")],
                "query_string": b"",
                "app": app,
                "client": ("testclient", 1234),
                "state": {},
            }
        )
        with patch(
            "provide.uterm.server.app.factory_impl.resolve_http_principal",
            AsyncMock(return_value=Principal(subject_id="fallback", roles=frozenset({"viewer"}))),
        ):
            await dep(conn)
        assert conn.state.uterm_principal.subject_id == "fallback"

    async def test_share_cookie_ip_binding_without_issued_ip_allows_token_validation(self) -> None:
        from provide.uterm.tunnel.token_hash import hash_token

        app, _ = _make_app(tunnel=TunnelConfig(ip_binding=True, token_transport="cookie"))
        app.state.uterm_tunnel_tokens["sess-ipfree"] = {
            "share_token_hash": hash_token("tok"),
            "control_token_hash": hash_token("control"),
            "expires_at": time.time() + 3600,
        }
        dep = _auth_dep(app)
        conn = HTTPConnection(
            {
                "type": "http",
                "path": "/api/sessions/sess-ipfree",
                "headers": [(b"cookie", b"uterm_tunnel_sess-ipfree=tok")],
                "query_string": b"",
                "app": app,
                "client": ("testclient", 1234),
                "state": {},
            }
        )

        await dep(conn)

        assert conn.state.uterm_principal.subject_id == "share:sess-ipfree:viewer"

    async def test_tunnel_ws_worker_principal_falls_back_to_resolver(self) -> None:
        app, _ = _make_app()
        dep = _auth_dep(app)
        conn = HTTPConnection(
            {
                "type": "websocket",
                "path": "/tunnel/w-missing",
                "headers": [(b"authorization", b"Bearer tok")],
                "query_string": b"",
                "app": app,
                "client": ("testclient", 1234),
                "state": {},
            }
        )
        with patch(
            "provide.uterm.server.app.factory_impl.resolve_ws_principal",
            AsyncMock(return_value=Principal(subject_id="ok", roles=frozenset({"viewer"}))),
        ):
            await dep(conn)
        assert conn.state.uterm_principal.subject_id == "ok"

    async def test_http_no_token_and_missing_token_state_paths(self) -> None:
        app, _ = _make_app()
        dep = _auth_dep(app)
        conn1 = HTTPConnection(
            {
                "type": "http",
                "path": "/api/sessions/s-miss",
                "headers": [],
                "query_string": b"",
                "app": app,
                "client": ("testclient", 1234),
                "state": {},
            }
        )
        with patch(
            "provide.uterm.server.app.factory_impl.resolve_http_principal",
            AsyncMock(return_value=Principal(subject_id="ok", roles=frozenset({"viewer"}))),
        ):
            await dep(conn1)
        assert conn1.state.uterm_principal.subject_id == "ok"

        conn2 = HTTPConnection(
            {
                "type": "http",
                "path": "/api/sessions/s-miss-state",
                "headers": [],
                "query_string": b"token=tok",
                "app": app,
                "client": ("testclient", 1234),
                "state": {},
            }
        )
        with patch(
            "provide.uterm.server.app.factory_impl.resolve_http_principal",
            AsyncMock(return_value=Principal(subject_id="ok2", roles=frozenset({"viewer"}))),
        ):
            await dep(conn2)
        assert conn2.state.uterm_principal.subject_id == "ok2"

    async def test_ws_worker_global_bearer_and_anonymous_rejection(self) -> None:
        app, cfg = _make_app()
        dep = _auth_dep(app)
        token = cfg.auth.worker_bearer_token
        conn_worker = HTTPConnection(
            {
                "type": "websocket",
                "path": "/ws/worker/wid/term",
                "headers": [(b"authorization", f"Bearer {token}".encode())],
                "query_string": b"",
                "app": app,
                "client": ("testclient", 1234),
                "state": {},
            }
        )
        await dep(conn_worker)
        assert conn_worker.state.uterm_principal.subject_id == "worker"

        from fastapi import WebSocketException

        conn_browser = HTTPConnection(
            {
                "type": "websocket",
                "path": "/ws/browser/wid/term",
                "headers": [],
                "query_string": b"",
                "app": app,
                "client": ("testclient", 1234),
                "state": {},
            }
        )
        with (
            patch(
                "provide.uterm.server.app.factory_impl.resolve_ws_principal",
                AsyncMock(return_value=Principal(subject_id="anonymous", roles=frozenset({"viewer"}))),
            ),
            pytest.raises(WebSocketException),
        ):
            await dep(conn_browser)


class TestFactoryClosures:
    async def test_on_resume_branches(self) -> None:
        app, _ = _make_app()
        hub = app.state.uterm_hub
        session = MagicMock(worker_id="sid", wall_created_at=time.time() - 100)
        app.state.uterm_registry.get_definition = AsyncMock(return_value=None)
        assert await hub._on_resume("tok", session) is False

        newer = MagicMock()
        newer.created_at.timestamp.return_value = time.time()
        app.state.uterm_registry.get_definition = AsyncMock(return_value=newer)
        assert await hub._on_resume("tok", session) is False

        older = MagicMock()
        older.created_at.timestamp.return_value = time.time() - 200
        app.state.uterm_registry.get_definition = AsyncMock(return_value=older)
        assert await hub._on_resume("tok", session) is True

    async def test_resolve_browser_role_branches(self) -> None:
        app, _ = _make_app()
        hub = app.state.uterm_hub
        ws = MagicMock()
        ws.state = MagicMock()
        ws.state.uterm_principal = Principal(subject_id="u", roles=frozenset({"admin"}))
        app.state.uterm_registry.get_definition = AsyncMock(return_value=None)
        assert await hub._resolve_browser_role(ws, "sid") == "admin"
        ws.state.uterm_principal = Principal(subject_id="u", roles=frozenset({"operator"}))
        assert await hub._resolve_browser_role(ws, "sid") == "operator"
        ws.state.uterm_principal = Principal(subject_id="u", roles=frozenset({"viewer"}))
        assert await hub._resolve_browser_role(ws, "sid") == "viewer"

    async def test_resolve_browser_role_resolves_missing_principal_and_session_authz(self) -> None:
        app, _ = _make_app()
        hub = app.state.uterm_hub
        ws = MagicMock()
        ws.state = MagicMock()
        if hasattr(ws.state, "uterm_principal"):
            delattr(ws.state, "uterm_principal")
        with patch(
            "provide.uterm.server.app.factory_impl.resolve_ws_principal",
            AsyncMock(return_value=Principal(subject_id="u2", roles=frozenset({"viewer"}))),
        ):
            app.state.uterm_registry.get_definition = AsyncMock(return_value=MagicMock())
            from fastapi import WebSocketException

            with patch(
                "provide.uterm.server.authorization.AuthorizationService.can_read_session",
                AsyncMock(return_value=False),
            ):
                with pytest.raises(WebSocketException):
                    await hub._resolve_browser_role(ws, "sid")
            with patch(
                "provide.uterm.server.authorization.AuthorizationService.can_read_session", AsyncMock(return_value=True)
            ):
                with patch(
                    "provide.uterm.server.policy.SessionPolicyResolver.role_for",
                    AsyncMock(return_value="viewer"),
                ):
                    assert await hub._resolve_browser_role(ws, "sid") == "viewer"


class TestRemainingFactoryCoverage:
    async def test_tunnel_ws_global_worker_token_and_ip_mismatch(self) -> None:
        app, cfg = _make_app(tunnel=TunnelConfig(ip_binding=True, token_transport="both"))
        dep = _auth_dep(app)
        token = cfg.auth.worker_bearer_token
        conn_global = HTTPConnection(
            {
                "type": "websocket",
                "path": "/tunnel/w-global",
                "headers": [(b"authorization", f"Bearer {token}".encode())],
                "query_string": b"",
                "app": app,
                "client": ("testclient", 1234),
                "state": {},
            }
        )
        await dep(conn_global)
        assert conn_global.state.uterm_principal.subject_id == "worker"

        app.state.uterm_tunnel_tokens["w-ipbad"] = {
            "worker_token_hash": "h",
            "expires_at": time.time() + 3600,
            "issued_ip": "10.10.10.10",
        }
        conn_ipbad = HTTPConnection(
            {
                "type": "websocket",
                "path": "/tunnel/w-ipbad",
                "headers": [(b"authorization", b"Bearer tok")],
                "query_string": b"",
                "app": app,
                "client": ("testclient", 1234),
                "state": {},
            }
        )
        with patch(
            "provide.uterm.server.app.factory_impl.resolve_ws_principal",
            AsyncMock(return_value=Principal(subject_id="fallback", roles=frozenset({"viewer"}))),
        ):
            await dep(conn_ipbad)
        assert conn_ipbad.state.uterm_principal.subject_id == "fallback"

    async def test_recording_retention_sweep_exception_path(self, tmp_path) -> None:
        rec_dir = tmp_path / "rec"
        rec_dir.mkdir()
        old = rec_dir / "old.jsonl"
        old.write_text("{}\n", encoding="utf-8")
        old_ts = time.time() - 7200
        os.utime(old, (old_ts, old_ts))
        app, _ = _make_app(
            recording=RecordingConfig(
                enabled_by_default=False,
                directory=rec_dir,
                retention_s=3600,
                store_type="local",
            )
        )
        with patch("pathlib.Path.unlink", side_effect=RuntimeError("unlink boom")):
            await _run_lifespan_one_tick(app)

    async def test_sqlite_durability_and_discovery_heartbeat_branch(self, tmp_path) -> None:
        cfg = default_server_config()
        cfg.sessions = []
        cfg.control_plane.backend = "sqlite"
        cfg.control_plane.database_url = f"sqlite:///{tmp_path / 'cp.db'}"
        cfg.governance.discovery_provider = "webhook"
        cfg.governance.registry_webhook_url = "https://discovery.example.test"
        cfg.governance.registry_webhook_interval_s = 1
        app = create_server_app(cfg)
        with patch(
            "provide.uterm.server.discovery.WebhookDiscoveryProvider.announce", AsyncMock(side_effect=RuntimeError("x"))
        ):
            await _run_lifespan_one_tick(app)
