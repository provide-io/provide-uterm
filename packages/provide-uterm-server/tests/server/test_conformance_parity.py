#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Conformance slice — align FastAPI and CF behavior on session surfaces."""

from __future__ import annotations

from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.models import SessionDefinition


def _make_client() -> TestClient:
    cfg = default_server_config()
    cfg.auth.mode = "header"
    cfg.auth.header_mode_acknowledged = True
    cfg.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    cfg.sessions = [
        SessionDefinition(
            session_id="share-sess",
            display_name="Share Session",
            connector_type="shell",
        ),
        SessionDefinition(
            session_id="delete-sess",
            display_name="Delete Session",
            connector_type="shell",
        ),
    ]
    app = create_server_app(cfg)
    app.state.uterm_tunnel_tokens = {
        "share-sess": {
            "share_token": "share-token-123",
            "control_token": "control-token-123",
            "worker_token": "worker-token-123",
        }
    }
    return TestClient(app)


def test_share_token_is_not_exposed_in_html_bootstrap() -> None:
    with _make_client() as client:
        resp = client.get("/app/session/share-sess?token=share-token-123")

    assert resp.status_code == 200
    assert "share-token-123" not in resp.text
    assert '"share_token"' not in resp.text


def test_deleted_session_id_is_not_readable_after_delete() -> None:
    with _make_client() as client:
        delete_resp = client.delete("/api/sessions/delete-sess")
        after_resp = client.get("/api/sessions/delete-sess")

    assert delete_resp.status_code == 200
    assert after_resp.status_code == 404
