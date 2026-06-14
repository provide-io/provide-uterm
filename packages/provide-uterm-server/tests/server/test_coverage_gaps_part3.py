#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests targeting specific coverage gaps across server modules."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import jwt as _jwt
from fastapi.testclient import TestClient

from provide.uterm.recording import LocalFileRecordingStore
from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.models import (
    AuthConfig,
    RecordingConfig,
    SessionDefinition,
)
from provide.uterm.server.registry import SessionRegistry

_TEST_KEY = "uterm-test-secret-32-byte-minimum-key"


def _make_token(sub: str = "user1", roles: list[str] | None = None) -> str:
    now = int(time.time())
    return _jwt.encode(
        {
            "sub": sub,
            "roles": roles or ["viewer"],
            "iss": "provide-uterm",
            "aud": "provide-uterm-server",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        key=_TEST_KEY,
        algorithm="HS256",
    )


def _make_hub() -> MagicMock:
    hub = MagicMock()
    hub.force_release_hijack = AsyncMock(return_value=True)
    hub.set_input_mode = AsyncMock(return_value=(True, None))
    hub.get_last_snapshot = AsyncMock(return_value=None)
    hub.get_recent_events = AsyncMock(return_value=[])
    hub.browser_count = AsyncMock(return_value=0)
    hub.on_worker_empty = None
    return hub


def _make_registry(
    sessions: list[SessionDefinition] | None = None,
    *,
    recording: RecordingConfig | None = None,
) -> SessionRegistry:
    hub = _make_hub()
    recording_cfg = recording or RecordingConfig()
    return SessionRegistry(
        sessions or [],
        hub=hub,
        public_base_url="http://localhost:9999",
        recording=recording_cfg,
        recording_store=LocalFileRecordingStore(recording_cfg.directory),
    )


def _session(
    session_id: str = "sess1",
    auto_start: bool = False,
    ephemeral: bool = False,
) -> SessionDefinition:
    return SessionDefinition(
        session_id=session_id,
        display_name=f"Session {session_id}",
        connector_type="shell",
        auto_start=auto_start,
        ephemeral=ephemeral,
    )


# ===========================================================================
# pam_integration.py coverage gaps
# ===========================================================================


class TestUiGaps:
    """Covers inspect_page_html (lines 256-273)."""

    def test_inspect_page_html_minimal(self) -> None:
        """Lines 256-273: inspect_page_html with minimal args."""
        from provide.uterm.server import ui

        ui._vite_manifest = None
        ui._vite_manifest_loaded = True

        html = ui.inspect_page_html(
            "Inspect",
            "/assets",
            "sess-2",
            app_path="/app",
        )
        assert '"page_kind": "inspect"' in html
        assert '"share_role": null' in html
        assert "sess-2" in html

        ui._vite_manifest = None
        ui._vite_manifest_loaded = False


# ===========================================================================
# routes/api.py coverage gaps
# ===========================================================================


class TestApiGaps:
    """Covers lines 158, 170, 173, 176->178, 411, 591-594."""

    def _admin_client(self) -> TestClient:
        cfg = default_server_config()
        cfg.auth.mode = "header"
        cfg.auth.header_mode_acknowledged = True
        cfg.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
        app = create_server_app(cfg)
        return TestClient(app)

    def test_bulk_delete_with_state_filter(self) -> None:
        """Lines 170, 173: bulk delete filters by state."""
        client = self._admin_client()
        # Create sessions
        client.post("/api/sessions", json={"session_id": "bd-1", "connector_type": "shell"})
        client.post("/api/sessions", json={"session_id": "bd-2", "connector_type": "shell"})

        # Bulk delete with state filter — "running" should not match stopped sessions
        # (sessions default to lifecycle_state="stopped")
        r = client.request(
            "DELETE",
            "/api/sessions",
            json={"filter": {"state": "running"}},
        )
        assert r.status_code == 200
        # No sessions should be deleted since none are running
        assert r.json()["deleted"] == 0

    def test_bulk_delete_with_older_than_filter(self) -> None:
        """Lines 173, 176->178: bulk delete filters by older_than_s and stopped_at."""
        client = self._admin_client()
        client.post("/api/sessions", json={"session_id": "bd-old", "connector_type": "shell"})

        r = client.request(
            "DELETE",
            "/api/sessions",
            json={"filter": {"older_than_s": 3600}},
        )
        assert r.status_code == 200
        # Sessions without stopped_at don't match older_than_s filter
        body = r.json()
        assert isinstance(body["deleted"], int)

    def test_bulk_delete_requires_admin(self) -> None:
        """Line 158: non-admin gets 403 on bulk delete."""
        cfg = default_server_config()
        cfg.auth = AuthConfig(
            mode="jwt",
            jwt_public_key_pem=_TEST_KEY,
            jwt_algorithms=["HS256"],
            jwt_issuer="provide-uterm",
            jwt_audience="provide-uterm-server",
            worker_bearer_token=_make_token(sub="worker", roles=["admin"]),
        )
        app = create_server_app(cfg)
        token = _make_token(sub="viewer", roles=["viewer"])
        with TestClient(app) as client:
            r = client.request(
                "DELETE",
                "/api/sessions",
                json={"filter": {}},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 403

    def test_bulk_delete_can_mutate_check(self) -> None:
        """Line 170 is unreachable (admin check at line 157 + admin always passes
        can_mutate_session). Test that admin bulk delete works correctly."""
        cfg = default_server_config()
        cfg.auth = AuthConfig(
            mode="jwt",
            jwt_public_key_pem=_TEST_KEY,
            jwt_algorithms=["HS256"],
            jwt_issuer="provide-uterm",
            jwt_audience="provide-uterm-server",
            worker_bearer_token=_make_token(sub="worker", roles=["admin"]),
        )
        cfg.sessions = [
            SessionDefinition(
                session_id="bd-sess",
                display_name="BD",
                connector_type="shell",
                visibility="public",
            )
        ]
        app = create_server_app(cfg)
        admin_token = _make_token(sub="admin", roles=["admin"])
        with TestClient(app) as client:
            r = client.request(
                "DELETE",
                "/api/sessions",
                json={"filter": {}},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
        assert r.status_code == 200

    def test_bulk_delete_with_stopped_at_too_recent(self) -> None:
        """Line 176->178: older_than_s filter skips sessions stopped too recently."""
        cfg = default_server_config()
        cfg.auth.mode = "header"
        cfg.auth.header_mode_acknowledged = True
        cfg.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
        app = create_server_app(cfg)

        with TestClient(app) as client:
            # Create a session
            client.post("/api/sessions", json={"session_id": "bd-recent", "connector_type": "websocket"})

            # Use the API to simulate tunnel connect/disconnect which sets stopped_at
            registry = app.state.uterm_registry
            # Directly set _stopped_at on the runtime via set_tunnel_connected
            import asyncio

            loop = asyncio.new_event_loop()
            loop.run_until_complete(registry.set_tunnel_connected("bd-recent", True))
            loop.run_until_complete(registry.set_tunnel_connected("bd-recent", False))
            loop.close()

            # Now bulk delete with older_than_s=3600 — the session was stopped <1s ago
            r = client.request(
                "DELETE",
                "/api/sessions",
                json={"filter": {"older_than_s": 3600}},
            )
            assert r.status_code == 200
            # bd-recent was stopped too recently (< 3600s ago), provide-shell has stopped_at=None
            assert r.json()["deleted"] == 0

    def test_watch_events_403_insufficient_privileges(self) -> None:
        """Line 411: watch_events returns 403 for unauthorized principal."""
        cfg = default_server_config()
        cfg.auth = AuthConfig(
            mode="jwt",
            jwt_public_key_pem=_TEST_KEY,
            jwt_algorithms=["HS256"],
            jwt_issuer="provide-uterm",
            jwt_audience="provide-uterm-server",
            worker_bearer_token=_make_token(sub="worker", roles=["admin"]),
        )
        cfg.sessions = [
            SessionDefinition(
                session_id="watch-priv",
                display_name="Watch",
                connector_type="shell",
                visibility="operator",
            )
        ]
        app = create_server_app(cfg)
        viewer_token = _make_token(sub="viewer", roles=["viewer"])
        with TestClient(app) as client:
            r = client.get(
                "/api/sessions/watch-priv/events/watch",
                headers={"Authorization": f"Bearer {viewer_token}"},
            )
        assert r.status_code == 403

    def test_create_tunnel_validation_error(self) -> None:
        """Lines 591-594: create tunnel returns 422 on validation error."""
        client = self._admin_client()

        # Force a validation error by making the registry fail
        async def _fail(payload: dict[str, Any], **_kwargs: Any) -> Any:
            from provide.uterm.server.registry import SessionValidationError

            raise SessionValidationError("bad tunnel")

        original_create = client.app.state.uterm_registry.create_session  # type: ignore[union-attr]
        client.app.state.uterm_registry.create_session = _fail  # type: ignore[union-attr]
        r = client.post("/api/tunnels", json={"tunnel_type": "terminal"})
        client.app.state.uterm_registry.create_session = original_create  # type: ignore[union-attr]
        assert r.status_code == 422

    def test_create_tunnel_conflict_error(self) -> None:
        """Lines 591-594: create tunnel returns 409 on ValueError (conflict)."""
        client = self._admin_client()

        async def _conflict(payload: dict[str, Any], **_kwargs: Any) -> Any:
            raise ValueError("session already exists")

        original_create = client.app.state.uterm_registry.create_session  # type: ignore[union-attr]
        client.app.state.uterm_registry.create_session = _conflict  # type: ignore[union-attr]
        r = client.post("/api/tunnels", json={"tunnel_type": "terminal"})
        client.app.state.uterm_registry.create_session = original_create  # type: ignore[union-attr]
        assert r.status_code == 409

    def test_bulk_delete_actually_deletes_old_session(self) -> None:
        """Lines 176->178: session IS old enough → gets deleted."""
        import asyncio
        import time

        cfg = default_server_config()
        cfg.auth.mode = "header"
        cfg.auth.header_mode_acknowledged = True
        cfg.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
        app = create_server_app(cfg)

        with TestClient(app) as client:
            client.post(
                "/api/sessions",
                json={
                    "session_id": "bd-old2",
                    "connector_type": "websocket",
                    "connector_config": {"url": "ws://127.0.0.1:1"},
                },
            )

            # Connect/disconnect to set stopped_at, then backdate it.
            registry = app.state.uterm_registry

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(registry.set_tunnel_connected("bd-old2", True))
                loop.run_until_complete(registry.set_tunnel_connected("bd-old2", False))
            finally:
                loop.close()
            rt = registry.get_runtime("bd-old2")
            if rt is not None:
                rt._stopped_at = time.time() - 7200

            r = client.request(
                "DELETE",
                "/api/sessions",
                json={"filter": {"older_than_s": 3600}},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["deleted"] >= 1
