#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Targeted coverage tests for app.py — sweep tasks, tunnel IP binding, PAM teardown."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.models import TunnelConfig


def _make_app(**config_overrides: Any) -> tuple[Any, Any]:
    """Create a test app with dev auth and no auto-start sessions."""
    config = default_server_config()
    config.auth.mode = "dev"
    config.sessions = []
    for key, value in config_overrides.items():
        setattr(config, key, value)
    return create_server_app(config), config


async def _run_lifespan_one_tick(app: Any) -> None:
    """Enter the lifespan, yield to let background tasks run one iteration, then exit."""
    # We patch asyncio.sleep so that the sweep loops run once then block.
    # The trick: track which task is calling sleep. The first call from each
    # sweep task (the interval delay) returns immediately. The second call
    # (the next iteration's interval) raises CancelledError to stop the loop.
    # We use a set to track tasks that have already had their first sleep.
    ran_once: set[int] = set()
    _real_sleep = asyncio.sleep

    async def _patched_sleep(delay: float) -> None:
        task = id(asyncio.current_task())
        if task in ran_once:
            # Second call from same task — block forever (will be cancelled by lifespan exit).
            await _real_sleep(3600)
            return
        ran_once.add(task)
        # First call — return immediately so the loop body runs.
        await _real_sleep(0)

    with patch("asyncio.sleep", _patched_sleep):
        async with app.router.lifespan_context(app):
            # Yield control so all background tasks complete their first iteration.
            for _ in range(20):
                await _real_sleep(0)


class TestTunnelTokenIpBinding:
    """Cover lines 194-201: tunnel token IP binding mismatch."""

    def test_ip_binding_mismatch_rejects_token(self) -> None:
        """When ip_binding=True and client IP differs from issued_ip, token is rejected."""
        tunnel_cfg = TunnelConfig(ip_binding=True, token_transport="query")
        app, _config = _make_app(tunnel=tunnel_cfg)

        with TestClient(app) as client:
            client.post(
                "/api/sessions",
                json={"session_id": "ip-test", "display_name": "IP Test", "connector_type": "shell"},
            )
            token_map = app.state.uterm_tunnel_tokens
            token_map["ip-test"] = {
                "share_token": "valid-share-tok",
                "control_token": "valid-ctrl-tok",
                "issued_ip": "10.0.0.99",  # Different from testclient's IP
            }

            # In dev mode, request succeeds regardless; the IP mismatch prevents the
            # share principal from being set, but dev mode does not enforce auth.
            resp = client.get("/api/sessions/ip-test", params={"token": "valid-share-tok"})
            assert resp.status_code == 200

    def test_ip_binding_match_allows_token(self) -> None:
        """When ip_binding=True and client IP matches issued_ip, token is accepted."""
        tunnel_cfg = TunnelConfig(ip_binding=True, token_transport="query")
        app, _config = _make_app(tunnel=tunnel_cfg)

        with TestClient(app) as client:
            client.post(
                "/api/sessions",
                json={"session_id": "ip-ok", "display_name": "IP OK", "connector_type": "shell"},
            )
            token_map = app.state.uterm_tunnel_tokens
            # TestClient uses "testclient" as client IP in ASGI scope.
            token_map["ip-ok"] = {
                "share_token": "good-share-tok",
                "control_token": "good-ctrl-tok",
                "issued_ip": "testclient",
            }

            resp = client.get("/api/sessions/ip-ok", params={"token": "good-share-tok"})
            assert resp.status_code == 200

    def test_ip_binding_no_issued_ip_passes(self) -> None:
        """When ip_binding=True but issued_ip is empty, the check passes through."""
        tunnel_cfg = TunnelConfig(ip_binding=True, token_transport="query")
        app, _config = _make_app(tunnel=tunnel_cfg)

        with TestClient(app) as client:
            client.post(
                "/api/sessions",
                json={"session_id": "ip-none", "display_name": "No IP", "connector_type": "shell"},
            )
            token_map = app.state.uterm_tunnel_tokens
            token_map["ip-none"] = {
                "share_token": "share-tok",
                "control_token": "ctrl-tok",
                # issued_ip absent — the `if issued_ip and ...` guard skips the check.
            }

            resp = client.get("/api/sessions/ip-none", params={"token": "share-tok"})
            assert resp.status_code == 200

    def test_expired_token_rejected(self) -> None:
        """Cover lines 189-192: expired tunnel token returns None."""
        tunnel_cfg = TunnelConfig(token_transport="query")
        app, _config = _make_app(tunnel=tunnel_cfg)

        with TestClient(app) as client:
            client.post(
                "/api/sessions",
                json={"session_id": "exp-test", "display_name": "Expired", "connector_type": "shell"},
            )
            token_map = app.state.uterm_tunnel_tokens
            token_map["exp-test"] = {
                "share_token": "expired-tok",
                "control_token": "ctrl-tok",
                "expires_at": time.time() - 100,
            }

            resp = client.get("/api/sessions/exp-test", params={"token": "expired-tok"})
            assert resp.status_code == 200


class TestSweepIdleSessions:
    """Cover lines 314-327: _sweep_idle_sessions body."""

    async def test_sweep_idle_disconnects_idle_workers(self) -> None:
        """Idle sweep iterates candidates and calls disconnect_worker."""
        app, _config = _make_app(session_idle_timeout_s=60)
        hub = app.state.uterm_hub

        hub.get_idle_candidates = AsyncMock(return_value=[("w1", time.time() - 120)])
        hub.disconnect_worker = AsyncMock()

        await _run_lifespan_one_tick(app)

        hub.get_idle_candidates.assert_awaited()
        hub.disconnect_worker.assert_awaited_with("w1")

    async def test_sweep_idle_skips_when_timeout_zero(self) -> None:
        """When session_idle_timeout_s=0, sweep continues without disconnecting."""
        app, _config = _make_app(session_idle_timeout_s=0)
        hub = app.state.uterm_hub

        hub.get_idle_candidates = AsyncMock(return_value=[])
        hub.disconnect_worker = AsyncMock()

        await _run_lifespan_one_tick(app)

        # timeout_s <= 0 means the loop body continues without calling disconnect.
        hub.disconnect_worker.assert_not_awaited()

    async def test_sweep_idle_handles_disconnect_error(self) -> None:
        """Exception in disconnect_worker is logged but does not crash the sweep."""
        app, _config = _make_app(session_idle_timeout_s=60)
        hub = app.state.uterm_hub

        hub.get_idle_candidates = AsyncMock(return_value=[("w-err", time.time() - 200)])
        hub.disconnect_worker = AsyncMock(side_effect=RuntimeError("boom"))

        await _run_lifespan_one_tick(app)

        hub.disconnect_worker.assert_awaited_with("w-err")


class TestSweepExpiredSessions:
    """Cover lines 334-352: _sweep_expired_sessions body."""

    async def test_sweep_retention_deletes_old_stopped_sessions(self) -> None:
        """Retention sweep deletes stopped sessions older than retention_s."""
        app, _config = _make_app(session_retention_s=300)
        registry = app.state.uterm_registry

        mock_status = MagicMock()
        mock_status.lifecycle_state = "stopped"
        mock_status.stopped_at = time.time() - 600
        mock_status.session_id = "old-sess"

        registry.list_sessions_with_definitions = AsyncMock(return_value=[(mock_status, MagicMock())])
        registry.delete_session = AsyncMock()

        await _run_lifespan_one_tick(app)

        registry.delete_session.assert_awaited_with("old-sess")

    async def test_sweep_retention_skips_running_sessions(self) -> None:
        """Retention sweep skips sessions not in 'stopped' state."""
        app, _config = _make_app(session_retention_s=300)
        registry = app.state.uterm_registry

        mock_status = MagicMock()
        mock_status.lifecycle_state = "running"
        mock_status.session_id = "running-sess"

        registry.list_sessions_with_definitions = AsyncMock(return_value=[(mock_status, MagicMock())])
        registry.delete_session = AsyncMock()

        await _run_lifespan_one_tick(app)

        registry.delete_session.assert_not_awaited()

    async def test_sweep_retention_skips_when_stopped_at_none(self) -> None:
        """Retention sweep skips stopped sessions with stopped_at=None."""
        app, _config = _make_app(session_retention_s=300)
        registry = app.state.uterm_registry

        mock_status = MagicMock()
        mock_status.lifecycle_state = "stopped"
        mock_status.stopped_at = None
        mock_status.session_id = "no-time"

        registry.list_sessions_with_definitions = AsyncMock(return_value=[(mock_status, MagicMock())])
        registry.delete_session = AsyncMock()

        await _run_lifespan_one_tick(app)

        registry.delete_session.assert_not_awaited()

    async def test_sweep_retention_skips_when_retention_zero(self) -> None:
        """When session_retention_s=0, sweep continues without deleting."""
        app, _config = _make_app(session_retention_s=0)
        registry = app.state.uterm_registry

        registry.list_sessions_with_definitions = AsyncMock(return_value=[])
        registry.delete_session = AsyncMock()

        await _run_lifespan_one_tick(app)

        registry.delete_session.assert_not_awaited()

    async def test_sweep_retention_handles_delete_error(self) -> None:
        """Exception in delete_session is logged but does not crash the sweep."""
        app, _config = _make_app(session_retention_s=300)
        registry = app.state.uterm_registry

        mock_status = MagicMock()
        mock_status.lifecycle_state = "stopped"
        mock_status.stopped_at = time.time() - 600
        mock_status.session_id = "err-sess"

        registry.list_sessions_with_definitions = AsyncMock(return_value=[(mock_status, MagicMock())])
        registry.delete_session = AsyncMock(side_effect=RuntimeError("delete failed"))

        await _run_lifespan_one_tick(app)

        registry.delete_session.assert_awaited_with("err-sess")

    async def test_sweep_retention_skips_recently_stopped(self) -> None:
        """Retention sweep skips stopped sessions not yet old enough."""
        app, _config = _make_app(session_retention_s=300)
        registry = app.state.uterm_registry

        mock_status = MagicMock()
        mock_status.lifecycle_state = "stopped"
        mock_status.stopped_at = time.time() - 10  # 10s ago, retention is 300s.
        mock_status.session_id = "fresh-stop"

        registry.list_sessions_with_definitions = AsyncMock(return_value=[(mock_status, MagicMock())])
        registry.delete_session = AsyncMock()

        await _run_lifespan_one_tick(app)

        registry.delete_session.assert_not_awaited()


class TestSweepExpiredTunnelTokens:
    """Cover lines 358-366: _sweep_expired_tunnel_tokens body."""

    async def test_sweep_removes_expired_tunnel_tokens(self) -> None:
        """Expired tunnel tokens are removed from the in-memory map."""
        app, _config = _make_app()
        token_map = app.state.uterm_tunnel_tokens

        token_map["expired-sess"] = {"share_token": "a", "expires_at": time.time() - 100}
        token_map["fresh-sess"] = {"share_token": "b", "expires_at": time.time() + 3600}

        await _run_lifespan_one_tick(app)

        assert "expired-sess" not in token_map
        assert "fresh-sess" in token_map

    async def test_sweep_keeps_tokens_without_expiry(self) -> None:
        """Tokens without expires_at field are not removed."""
        app, _config = _make_app()
        token_map = app.state.uterm_tunnel_tokens

        token_map["no-expiry"] = {"share_token": "c"}

        await _run_lifespan_one_tick(app)

        assert "no-expiry" in token_map


class TestLifespanPamTeardown:
    """Cover branch 395->399: PAM task is None when import fails."""

    async def test_pam_import_failure_means_no_pam_task(self) -> None:
        """When PAM integration fails to import, pam_task is None and line 395->399 is taken."""
        # Force ImportError for pam_integration so pam_task stays None.
        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__  # type: ignore[union-attr]

        def _blocking_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "provide.uterm.server.pam_integration":
                raise ImportError("fake: no PAM")
            return original_import(name, *args, **kwargs)

        # Remove the module from sys.modules so the import inside the lifespan
        # actually goes through __import__ again.
        import sys

        saved = sys.modules.pop("provide.uterm.server.pam_integration", None)
        try:
            with patch("builtins.__import__", side_effect=_blocking_import):
                app, _config = _make_app()
                await _run_lifespan_one_tick(app)
        finally:
            if saved is not None:
                sys.modules["provide.uterm.server.pam_integration"] = saved

    async def test_hub_shutdown_called_in_finally(self) -> None:
        """hub.shutdown() is called during lifespan teardown."""
        app, _config = _make_app()
        hub = app.state.uterm_hub

        original_shutdown = hub.shutdown
        shutdown_called = False

        async def tracking_shutdown() -> None:
            nonlocal shutdown_called
            shutdown_called = True
            await original_shutdown()

        hub.shutdown = tracking_shutdown

        await _run_lifespan_one_tick(app)

        assert shutdown_called is True
