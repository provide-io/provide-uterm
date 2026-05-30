#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Coverage gap tests for server app, connectors, and policy."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from provide.uterm.recording import LocalFileRecordingStore
from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.auth import Principal
from provide.uterm.server.connectors import TelnetSessionConnector, build_connector
from provide.uterm.server.models import RecordingConfig
from provide.uterm.server.registry import SessionRegistry

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# connectors/__init__.py:48 — build_connector("telnet") returns TelnetSessionConnector
# connectors/__init__.py:53 — unsupported type raises ValueError
# ---------------------------------------------------------------------------


def test_build_connector_telnet_returns_telnet_connector() -> None:
    conn = build_connector("sid", "Display", "telnet", {"host": "127.0.0.1", "port": 23})
    assert isinstance(conn, TelnetSessionConnector)


def test_build_connector_unsupported_type_raises() -> None:
    with pytest.raises(ValueError, match="unsupported connector_type"):
        build_connector("sid", "Display", "bogus", {})


# ---------------------------------------------------------------------------
# app.py:65-66 — _validate_frontend_assets raises when assets are missing
# ---------------------------------------------------------------------------


def test_validate_frontend_assets_raises_when_missing() -> None:
    from provide.uterm.server.app import _validate_frontend_assets

    class _FakePath:
        def __truediv__(self, name: str) -> _FakePath:
            return _FakePath()

        def is_file(self) -> bool:
            return False

    class _FakeRoot:
        def __truediv__(self, name: str) -> _FakePath:
            return _FakePath()

    with (
        patch("provide.uterm.server.app.importlib.resources.files", return_value=_FakeRoot()),
        pytest.raises(RuntimeError, match="missing required frontend assets"),
    ):
        _validate_frontend_assets()


# ---------------------------------------------------------------------------
# app.py:77 — _validate_auth_config returns early for non-jwt authenticated mode
# ---------------------------------------------------------------------------


def test_validate_auth_config_header_mode_passes() -> None:
    """Mode 'header' with acknowledged flag and worker_bearer_token should not raise."""
    from provide.uterm.server.app import _validate_auth_config

    cfg = default_server_config()
    cfg.auth.mode = "header"
    cfg.auth.header_mode_acknowledged = True
    cfg.auth.worker_bearer_token = "test-token"
    # Should not raise — header mode acknowledged and worker token set
    _validate_auth_config(cfg)


# ---------------------------------------------------------------------------
# app.py:154 — _resolve_browser_role falls back to resolve_ws_principal
#              when ws.state has no uterm_principal — defaults to viewer for
#              unknown sessions now that dev/none auth modes are gone.
# ---------------------------------------------------------------------------


async def test_resolve_browser_role_no_principal_unknown_session_returns_viewer() -> None:
    """_resolve_browser_role resolves principal when ws.state lacks uterm_principal.

    With allow_adhoc_browser_observers=True, a viewer principal resolved via the
    local IDP returns 'viewer' for an unregistered worker.  This covers the
    local-IDP branch that calls resolve_ws_principal() instead of the webhook path.
    """
    cfg = default_server_config()
    cfg.auth.mode = "header"
    cfg.auth.header_mode_acknowledged = True
    cfg.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    # Opt in so the viewer role is honored for ad-hoc (unregistered) workers.
    cfg.auth.allow_adhoc_browser_observers = True
    app = create_server_app(cfg)
    hub = app.state.uterm_hub

    mock_ws = SimpleNamespace(
        state=SimpleNamespace(),
        headers={},
        cookies={},
        scope={"type": "websocket", "headers": []},
    )
    role = await hub._resolve_browser_role(mock_ws, "nonexistent-worker")
    assert role == "viewer"


# ---------------------------------------------------------------------------
# app.py:159 — WebSocketException when can_read_session returns False
# ---------------------------------------------------------------------------


async def test_resolve_browser_role_raises_on_access_denied() -> None:
    """_resolve_browser_role raises WebSocketException when can_read_session is False."""
    from fastapi import WebSocketException

    cfg = default_server_config()
    cfg.auth.mode = "header"
    cfg.auth.header_mode_acknowledged = True
    cfg.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    app = create_server_app(cfg)
    hub = app.state.uterm_hub
    registry = app.state.uterm_registry

    # Create a private session
    await registry.create_session(
        {
            "session_id": "private-s",
            "connector_type": "shell",
            "visibility": "private",
            "owner": "alice",
        }
    )

    # Principal with no roles and no ownership → can_read_session = False
    principal_with_no_access = Principal(subject_id="stranger", roles=frozenset())
    mock_ws = SimpleNamespace(
        state=SimpleNamespace(uterm_principal=principal_with_no_access),
        headers={},
        cookies={},
        scope={"type": "websocket", "headers": []},
    )

    with pytest.raises(WebSocketException):
        await hub._resolve_browser_role(mock_ws, "private-s")


# ---------------------------------------------------------------------------
# app.py:227 — 5xx metric increment when response status >= 500
# ---------------------------------------------------------------------------


def test_5xx_metric_incremented_on_500_response() -> None:
    """Middleware increments http_requests_5xx_total when a route returns 500."""
    cfg = default_server_config()
    cfg.auth.mode = "header"
    cfg.auth.header_mode_acknowledged = True
    cfg.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    app = create_server_app(cfg)

    @app.get("/test-500-response")
    async def _return_500() -> JSONResponse:
        return JSONResponse({"error": "deliberate"}, status_code=500)

    with TestClient(app) as client:
        before = client.get("/api/metrics").json()["metrics"]
        client.get("/test-500-response")
        after = client.get("/api/metrics").json()["metrics"]

    assert after.get("http_requests_5xx_total", 0) > before.get("http_requests_5xx_total", 0)


# ---------------------------------------------------------------------------
# registry.py:68 — _on_worker_empty returns early when browsers still connected
# ---------------------------------------------------------------------------


async def test_on_worker_empty_skips_delete_when_browsers_remain() -> None:
    """Grace period: if browsers reconnect before timeout, ephemeral session is kept."""
    from unittest.mock import AsyncMock, MagicMock

    hub = MagicMock()
    hub.browser_count = AsyncMock(return_value=1)  # still has a browser
    hub.on_worker_empty = None
    recording = RecordingConfig()
    reg = SessionRegistry(
        [],
        hub=hub,
        public_base_url="http://localhost:9999",
        recording=recording,
        recording_store=LocalFileRecordingStore(recording.directory),
    )
    await reg.create_session(
        {
            "session_id": "eph",
            "connector_type": "shell",
            "ephemeral": True,
        }
    )

    # Patch sleep to avoid actual 5-second wait
    with patch("asyncio.sleep"):
        await reg._on_worker_empty("eph")

    # Session should still exist because browser_count > 0
    session = await reg.get_definition("eph")
    assert session is not None


# ---------------------------------------------------------------------------
# registry.py — _on_worker_empty identity check: session replaced during grace
# ---------------------------------------------------------------------------


async def test_on_worker_empty_skips_delete_when_session_replaced() -> None:
    """If the session is deleted and re-created during the grace sleep, keep the new one."""
    from unittest.mock import AsyncMock, MagicMock

    hub = MagicMock()
    hub.browser_count = AsyncMock(return_value=0)
    hub.on_worker_empty = None
    recording = RecordingConfig()
    reg = SessionRegistry(
        [],
        hub=hub,
        public_base_url="http://localhost:9999",
        recording=recording,
        recording_store=LocalFileRecordingStore(recording.directory),
    )
    await reg.create_session({"session_id": "eph", "connector_type": "shell", "ephemeral": True})

    async def _sleep_and_replace(_s: float) -> None:
        # Simulate the session being deleted and re-created under the same ID
        # while the grace period sleep is in progress.
        await reg.delete_session("eph")
        await reg.create_session({"session_id": "eph", "connector_type": "shell", "ephemeral": True})

    with patch("asyncio.sleep", side_effect=_sleep_and_replace):
        await reg._on_worker_empty("eph")

    # New session with the same ID should still be present (identity check protected it).
    assert await reg.get_definition("eph") is not None
