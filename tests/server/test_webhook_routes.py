#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for webhook REST routes (POST/GET/DELETE /api/sessions/{id}/webhooks)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from provide.terminal.server.app import create_server_app
from provide.terminal.server.config import config_from_mapping

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}
VIEWER_H = {"X-Uterm-Principal": "viewer-user", "X-Uterm-Role": "viewer"}


def _make_app(sessions: list | None = None) -> TestClient:
    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 8780},
            "auth": {"mode": "dev"},
            "sessions": sessions
            or [
                {
                    "session_id": "s1",
                    "display_name": "S1",
                    "connector_type": "shell",
                    "auto_start": False,
                }
            ],
        }
    )
    app = create_server_app(cfg)
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /api/sessions/{session_id}/webhooks
# ---------------------------------------------------------------------------


def test_register_webhook_ok() -> None:
    client = _make_app()
    with client:
        resp = client.post(
            "/api/sessions/s1/webhooks",
            json={"url": "https://example.com/hook"},
            headers=ADMIN_H,
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "s1"
    assert data["url"] == "https://example.com/hook"
    assert "webhook_id" in data
    assert data["event_types"] is None
    assert data["pattern"] is None


def test_register_webhook_with_options() -> None:
    client = _make_app()
    with client:
        resp = client.post(
            "/api/sessions/s1/webhooks",
            json={
                "url": "https://example.com/hook",
                "event_types": ["snapshot"],
                "pattern": r"\$ ",
                "secret": "mysecret",
            },
            headers=ADMIN_H,
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["event_types"] == ["snapshot"]
    assert data["pattern"] == r"\$ "
    # secret is never returned in the response


def test_register_webhook_404_unknown_session() -> None:
    client = _make_app()
    with client:
        resp = client.post(
            "/api/sessions/no-such/webhooks",
            json={"url": "https://example.com/hook"},
            headers=ADMIN_H,
        )
    assert resp.status_code == 404


def test_register_webhook_403_viewer_private_session() -> None:
    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 8780},
            "auth": {"mode": "dev"},
            "sessions": [
                {
                    "session_id": "private",
                    "display_name": "Private",
                    "connector_type": "shell",
                    "visibility": "private",
                    "auto_start": False,
                }
            ],
        }
    )
    app = create_server_app(cfg)
    with TestClient(app) as client:
        resp = client.post(
            "/api/sessions/private/webhooks",
            json={"url": "https://example.com/hook"},
            headers=VIEWER_H,
        )
    assert resp.status_code == 403


def test_register_webhook_422_missing_url() -> None:
    client = _make_app()
    with client:
        resp = client.post(
            "/api/sessions/s1/webhooks",
            json={"event_types": ["snapshot"]},
            headers=ADMIN_H,
        )
    assert resp.status_code == 422


def test_register_webhook_422_bad_event_types_type() -> None:
    client = _make_app()
    with client:
        resp = client.post(
            "/api/sessions/s1/webhooks",
            json={"url": "https://example.com/hook", "event_types": "snapshot"},
            headers=ADMIN_H,
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/sessions/{session_id}/webhooks
# ---------------------------------------------------------------------------


def test_list_webhooks_empty() -> None:
    client = _make_app()
    with client:
        resp = client.get("/api/sessions/s1/webhooks", headers=ADMIN_H)
    assert resp.status_code == 200
    assert resp.json() == {"webhooks": []}


def test_list_webhooks_returns_registered() -> None:
    client = _make_app()
    with client:
        client.post(
            "/api/sessions/s1/webhooks",
            json={"url": "https://example.com/a"},
            headers=ADMIN_H,
        )
        client.post(
            "/api/sessions/s1/webhooks",
            json={"url": "https://example.com/b"},
            headers=ADMIN_H,
        )
        resp = client.get("/api/sessions/s1/webhooks", headers=ADMIN_H)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["webhooks"]) == 2
    urls = {w["url"] for w in data["webhooks"]}
    assert urls == {"https://example.com/a", "https://example.com/b"}


def test_list_webhooks_404_unknown_session() -> None:
    client = _make_app()
    with client:
        resp = client.get("/api/sessions/no-such/webhooks", headers=ADMIN_H)
    assert resp.status_code == 404


def test_list_webhooks_403_insufficient_privileges() -> None:
    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 8780},
            "auth": {"mode": "dev"},
            "sessions": [
                {
                    "session_id": "private",
                    "display_name": "Private",
                    "connector_type": "shell",
                    "visibility": "private",
                    "auto_start": False,
                }
            ],
        }
    )
    app = create_server_app(cfg)
    with TestClient(app) as client:
        resp = client.get("/api/sessions/private/webhooks", headers=VIEWER_H)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# DELETE /api/sessions/{session_id}/webhooks/{webhook_id}
# ---------------------------------------------------------------------------


def test_unregister_webhook_ok() -> None:
    client = _make_app()
    with client:
        reg = client.post(
            "/api/sessions/s1/webhooks",
            json={"url": "https://example.com/hook"},
            headers=ADMIN_H,
        )
        webhook_id = reg.json()["webhook_id"]
        resp = client.delete(f"/api/sessions/s1/webhooks/{webhook_id}", headers=ADMIN_H)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["webhook_id"] == webhook_id


def test_unregister_webhook_404_unknown_webhook() -> None:
    client = _make_app()
    with client:
        resp = client.delete("/api/sessions/s1/webhooks/nonexistent", headers=ADMIN_H)
    assert resp.status_code == 404


def test_unregister_webhook_404_session_mismatch() -> None:
    """Cannot unregister a webhook registered on a different session."""
    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 8780},
            "auth": {"mode": "dev"},
            "sessions": [
                {"session_id": "s1", "display_name": "S1", "connector_type": "shell", "auto_start": False},
                {"session_id": "s2", "display_name": "S2", "connector_type": "shell", "auto_start": False},
            ],
        }
    )
    app = create_server_app(cfg)
    with TestClient(app) as client:
        reg = client.post(
            "/api/sessions/s1/webhooks",
            json={"url": "https://example.com/hook"},
            headers=ADMIN_H,
        )
        webhook_id = reg.json()["webhook_id"]
        # Try to unregister s1's webhook via s2's route
        resp = client.delete(f"/api/sessions/s2/webhooks/{webhook_id}", headers=ADMIN_H)
    assert resp.status_code == 404


def test_unregister_webhook_404_unknown_session() -> None:
    client = _make_app()
    with client:
        resp = client.delete("/api/sessions/no-such/webhooks/abc", headers=ADMIN_H)
    assert resp.status_code == 404


def test_unregister_webhook_403_insufficient_privileges() -> None:
    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 8780},
            "auth": {"mode": "dev"},
            "sessions": [
                {
                    "session_id": "private",
                    "display_name": "Private",
                    "connector_type": "shell",
                    "visibility": "private",
                    "auto_start": False,
                }
            ],
        }
    )
    app = create_server_app(cfg)
    with TestClient(app) as client:
        resp = client.delete("/api/sessions/private/webhooks/abc", headers=VIEWER_H)
    assert resp.status_code == 403
