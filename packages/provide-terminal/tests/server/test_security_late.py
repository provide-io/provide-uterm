#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Fifth- and sixth-review security regression tests (split from test_server_security_regressions.py)."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.terminal.client import connect_test_ws
from provide.terminal.server import config_from_mapping, create_server_app, default_server_config

_TEST_SIGNING_KEY = "uterm-test-secret-32-byte-minimum-key"


def _jwt_headers(
    *, sub: str, roles: list[str], issuer: str = "provide-terminal", audience: str = "provide-terminal-server"
) -> dict[str, str]:
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": sub,
            "roles": roles,
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        key=_TEST_SIGNING_KEY,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _jwt_config():
    from provide.terminal.server.models import AuthConfig

    now = int(time.time())
    worker_token = jwt.encode(
        {
            "sub": "runtime-worker",
            "roles": ["admin"],
            "iss": "provide-terminal",
            "aud": "provide-terminal-server",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        key=_TEST_SIGNING_KEY,
        algorithm="HS256",
    )
    return AuthConfig(
        mode="jwt",
        jwt_public_key_pem=_TEST_SIGNING_KEY,
        jwt_algorithms=["HS256"],
        worker_bearer_token=worker_token,
    )


# --- Fifth-review regression tests ---


async def test_ssh_connector_start_passes_connect_timeout() -> None:
    """SshSessionConnector.start() must pass connect_timeout=30 to asyncssh.connect."""
    from unittest.mock import AsyncMock, MagicMock, patch

    pytest.importorskip("asyncssh", reason="asyncssh not installed")
    from provide.terminal.server.connectors.ssh import SshSessionConnector

    connector = SshSessionConnector("sess1", "Session 1", {"host": "localhost", "insecure_no_host_check": True})
    mock_process = MagicMock()
    mock_process.stdin = MagicMock()
    mock_process.stdout = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.create_process = AsyncMock(return_value=mock_process)

    with patch("asyncssh.connect", new_callable=AsyncMock, return_value=mock_conn) as mock_connect:
        await connector.start()
        _, kwargs = mock_connect.call_args
        assert kwargs.get("connect_timeout") == 30


async def test_ssh_connector_handle_input_uses_utf8() -> None:
    """SshSessionConnector.handle_input must encode input as UTF-8, not latin-1."""
    from unittest.mock import AsyncMock, MagicMock

    pytest.importorskip("asyncssh", reason="asyncssh not installed")
    from provide.terminal.server.connectors.ssh import SshSessionConnector

    connector = SshSessionConnector("sess1", "Session 1", {"host": "localhost", "insecure_no_host_check": True})
    mock_stdin = MagicMock()
    mock_stdin.drain = AsyncMock()
    connector._stdin = mock_stdin
    connector._connected = True

    await connector.handle_input("€test")
    written: bytes = mock_stdin.write.call_args[0][0]
    assert written == "€test".encode("utf-8", errors="replace")
    # latin-1 would mangle the euro sign; verify we're not using it
    assert written != "€test".encode("latin-1", errors="replace")


def test_config_from_mapping_rejects_unknown_connector_type() -> None:
    """config_from_mapping must raise ValueError for an unknown connector_type in [[sessions]]."""
    with pytest.raises(ValueError, match="invalid connector_type"):
        config_from_mapping(
            {
                "sessions": [
                    {"session_id": "sess1", "display_name": "S1", "connector_type": "bogus_connector"},
                ]
            }
        )


async def test_bridge_stops_on_permanent_http_error() -> None:
    """TermBridge._run must stop reconnecting on 401/403/404, not back off and retry."""
    from unittest.mock import MagicMock

    from provide.terminal.hijack.bridge import TermBridge

    class FakeStatusError(Exception):
        status_code = 403

    class _FakeCtx:
        async def __aenter__(self) -> None:
            raise FakeStatusError("Forbidden")

        async def __aexit__(self, *_: object) -> None:
            return None

    fake_ws = MagicMock()
    fake_ws.connect = MagicMock(return_value=_FakeCtx())

    bot = MagicMock()
    bot.session = None
    bridge = TermBridge(bot, "worker1", "http://localhost:9999")

    real_websockets = sys.modules.pop("websockets", None)
    sys.modules["websockets"] = fake_ws
    try:
        await bridge.start()
        for _ in range(50):
            await asyncio.sleep(0.02)
            if not bridge._running:
                break
    finally:
        if real_websockets is not None:
            sys.modules["websockets"] = real_websockets
        else:
            sys.modules.pop("websockets", None)

    assert not bridge._running, "bridge should have stopped after a permanent HTTP error"


def test_hijack_acquire_error_message_says_session_not_worker() -> None:
    """hijack_acquire must return 'for this session', not the previous 'for this worker', in error text."""
    from fastapi import APIRouter

    from provide.terminal.hijack.hub import TermHub
    from provide.terminal.hijack.routes import register_rest_routes

    hub = TermHub()
    router = APIRouter()
    register_rest_routes(hub, router)
    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as client:
        r = client.post("/worker/no-such-worker/hijack/acquire", json={"owner": "test"})
        assert r.status_code == 409
        body = r.json()
        assert "session" in body["error"].lower()
        assert "this worker" not in body["error"].lower()


# --- Sixth-review regression tests ---


def test_browser_handlers_error_message_says_session_not_worker() -> None:
    """Browser WS hijack error messages must say 'for this session', not 'for this worker'."""
    import provide.terminal.hijack.routes.browser_handlers as bh_module

    source = bh_module.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")
    assert "for this worker" not in text, "browser_handlers.py still has 'for this worker' in error strings"


async def test_runtime_stops_on_permanent_http_error() -> None:
    """HostedSessionRuntime._run must stop retrying on permanent HTTP 401/403/404."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.terminal.server.models import RecordingConfig, SessionDefinition
    from provide.terminal.server.runtime import HostedSessionRuntime

    session = SessionDefinition(session_id="s1", display_name="S1", connector_type="shell", auto_start=False)
    runtime = HostedSessionRuntime(session, public_base_url="http://localhost:9999", recording=RecordingConfig())

    class FakeStatusError(Exception):
        status_code = 401

    mock_connector = AsyncMock()
    mock_connector.is_connected = MagicMock(return_value=True)
    mock_connector.set_mode = AsyncMock(return_value=[])
    mock_connector.get_snapshot = AsyncMock(return_value={"type": "snapshot", "screen": "", "ts": 0})
    mock_connector.stop = AsyncMock()

    class _FakeCtx:
        async def __aenter__(self) -> None:
            raise FakeStatusError("Unauthorized")

        async def __aexit__(self, *_: object) -> None:
            return None

    fake_ws_mod = MagicMock()
    fake_ws_mod.connect = MagicMock(return_value=_FakeCtx())

    real_ws = sys.modules.pop("websockets", None)
    sys.modules["websockets"] = fake_ws_mod
    try:
        with patch("provide.terminal.server.runtime.build_connector", return_value=mock_connector):
            await runtime.start()
            for _ in range(50):
                await asyncio.sleep(0.02)
                if runtime._state == "error" and (runtime._task is None or runtime._task.done()):
                    break
    finally:
        if real_ws is not None:
            sys.modules["websockets"] = real_ws
        else:
            sys.modules.pop("websockets", None)

    assert runtime._task is not None and runtime._task.done(), (
        "runtime task should have exited after permanent HTTP 401"
    )
    assert runtime._last_error is not None, "last_error should be set after a permanent HTTP failure"


def test_pages_use_state_principal_not_double_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Page routes must use request.state.uterm_principal set by _require_authenticated
    and must not call resolve_http_principal a second time."""
    import provide.terminal.server.routes.pages as pages_module

    call_count = {"n": 0}
    original = pages_module.resolve_http_principal

    def _spy(request: object, auth: object) -> object:
        call_count["n"] += 1
        return original(request, auth)  # type: ignore[arg-type]

    monkeypatch.setattr(pages_module, "resolve_http_principal", _spy)

    config = default_server_config()
    config.auth.mode = "dev"
    app = create_server_app(config)
    with TestClient(app) as client:
        r = client.get(config.ui.app_path + "/")
        assert r.status_code == 200

    assert call_count["n"] == 0, (
        f"resolve_http_principal called {call_count['n']} time(s) — expected 0 (state principal reused)"
    )


# ---------------------------------------------------------------------------
# Connector config validation — unknown keys rejected at __init__ time
# ---------------------------------------------------------------------------


class TestConnectorConfigValidation:
    """Unknown connector_config keys raise ValueError at connector __init__ time."""

    def test_shell_rejects_unknown_keys(self) -> None:
        from provide.terminal.server.connectors.shell import ShellSessionConnector

        with pytest.raises(ValueError, match="unknown shell connector_config keys"):
            ShellSessionConnector("s1", "Shell", {"typo_key": "value"})

    def test_shell_accepts_input_mode(self) -> None:
        from provide.terminal.server.connectors.shell import ShellSessionConnector

        connector = ShellSessionConnector("s1", "Shell", {"input_mode": "hijack"})
        assert connector is not None

    def test_telnet_rejects_unknown_keys(self) -> None:
        from provide.terminal.server.connectors.telnet import TelnetSessionConnector

        with pytest.raises(ValueError, match="unknown telnet connector_config keys"):
            TelnetSessionConnector("s1", "Telnet", {"unknown_option": True})

    def test_ssh_rejects_unknown_keys(self) -> None:
        from provide.terminal.server.connectors.ssh import SshSessionConnector

        with pytest.raises(ValueError, match="unknown ssh connector_config keys"):
            SshSessionConnector(
                "s1",
                "SSH",
                {
                    "host": "127.0.0.1",
                    "insecure_no_host_check": True,
                    "totally_bogus_param": "oops",
                },
            )


# ---------------------------------------------------------------------------
# Worker bearer token scope — must only authenticate WebSocket connections
# ---------------------------------------------------------------------------


class TestWorkerBearerTokenScope:
    """Worker bearer token must NOT grant access to REST or page routes."""

    @staticmethod
    def _make_jwt_app():
        """Create a JWT-mode app with a raw (non-JWT) worker bearer token."""
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(
            mode="jwt",
            jwt_public_key_pem=_TEST_SIGNING_KEY,
            jwt_algorithms=["HS256"],
            worker_bearer_token="raw-worker-token-not-a-jwt",
        )
        cfg = default_server_config()
        cfg.auth = auth
        return create_server_app(cfg), auth

    def test_worker_bearer_token_rejected_on_rest_api(self) -> None:
        app, auth = self._make_jwt_app()
        with TestClient(app) as client:
            r = client.get("/api/sessions", headers={"Authorization": f"Bearer {auth.worker_bearer_token}"})
            assert r.status_code == 401, f"expected 401, got {r.status_code}"

    def test_worker_bearer_token_rejected_on_page_routes(self) -> None:
        app, auth = self._make_jwt_app()
        with TestClient(app) as client:
            r = client.get("/app/", headers={"Authorization": f"Bearer {auth.worker_bearer_token}"})
            assert r.status_code == 401, f"expected 401, got {r.status_code}"

    def test_wrong_worker_token_on_worker_ws_falls_through(self) -> None:
        """Wrong bearer token on /ws/worker/ must fall through to JWT auth (and fail)."""
        from starlette.websockets import WebSocketDisconnect

        app, _auth = self._make_jwt_app()
        with TestClient(app) as client:
            with (
                pytest.raises(WebSocketDisconnect) as exc_info,
                connect_test_ws(
                    client,
                    "/ws/worker/test-worker/term",
                    headers={"Authorization": "Bearer wrong-token"},
                ),
            ):
                pass
            assert exc_info.value.code == 1008

    def test_header_mode_logs_startup_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Header auth mode must log a warning about trusting proxy headers."""
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(
            mode="header",
            worker_bearer_token="header-mode-token",
        )
        cfg = default_server_config()
        cfg.auth = auth
        with caplog.at_level(logging.WARNING, logger="provide.terminal.server.app"):
            create_server_app(cfg)
        assert any("auth_mode=header" in r.message and "reverse proxy" in r.message for r in caplog.records)

    def test_worker_bearer_token_rejected_on_browser_ws(self) -> None:
        """Worker bearer token on a browser WS route must NOT grant admin — it's worker-route-scoped."""
        from starlette.websockets import WebSocketDisconnect

        app, auth = self._make_jwt_app()
        with TestClient(app) as client:
            with (
                pytest.raises(WebSocketDisconnect) as exc_info,
                connect_test_ws(
                    client,
                    "/ws/browser/test-worker/term",
                    headers={"Authorization": f"Bearer {auth.worker_bearer_token}"},
                ),
            ):
                pass
            # WS 1008 = policy violation (auth required), not 1000 (normal close)
            assert exc_info.value.code == 1008
