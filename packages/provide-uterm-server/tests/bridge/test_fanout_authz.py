#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Integration tests for fan-out REST routes and authorization."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config


def _make_app() -> Any:
    cfg = default_server_config()
    cfg.auth.mode = "dev"
    cfg.sessions = []
    return create_server_app(cfg)


class TestCreateGroupAndSend:
    def test_create_group_and_send(self) -> None:
        app = _make_app()
        client = TestClient(app)

        # Create a group with two (non-existent) worker IDs
        resp = client.post(
            "/api/fanout/groups",
            json={"name": "test-group", "worker_ids": ["w1", "w2"], "mode": "parallel"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "test-group"
        assert body["session_count"] == 2
        group_id = body["group_id"]

        # List groups — should include the one we created
        resp = client.get("/api/fanout/groups")
        assert resp.status_code == 200
        groups = resp.json()
        assert any(g["group_id"] == group_id for g in groups)

        # Send to group — workers are not connected so all should fail
        resp = client.post(
            f"/api/fanout/groups/{group_id}/send",
            json={"data": "echo hello\n"},
        )
        assert resp.status_code == 200
        result = resp.json()
        assert result["group_id"] == group_id
        assert len(result["results"]) == 2
        assert all(not r["ok"] for r in result["results"])
        assert set(result["failed_sessions"]) == {"w1", "w2"}

        # Delete group
        resp = client.delete(f"/api/fanout/groups/{group_id}")
        assert resp.status_code == 204

        # Confirm deletion
        resp = client.get("/api/fanout/groups")
        assert resp.status_code == 200
        assert not any(g["group_id"] == group_id for g in resp.json())


class TestCreateGroupExceedsMaxSize:
    def test_create_group_exceeds_max_size(self) -> None:
        app = _make_app()
        client = TestClient(app)

        # Default max_group_size is 50; send 60 workers
        worker_ids = [f"w{i}" for i in range(60)]
        resp = client.post(
            "/api/fanout/groups",
            json={"name": "big-group", "worker_ids": worker_ids},
        )
        assert resp.status_code == 400
        assert "exceeds max" in resp.json()["error"].lower()


class TestGrantAccess:
    def test_grant_access(self) -> None:
        app = _make_app()
        client = TestClient(app)

        # Create group
        resp = client.post(
            "/api/fanout/groups",
            json={"name": "grant-test", "worker_ids": ["w1"]},
        )
        assert resp.status_code == 200
        group_id = resp.json()["group_id"]

        # Grant access to another principal
        resp = client.post(
            f"/api/fanout/groups/{group_id}/grants",
            json={"grantee": "other-user"},
        )
        assert resp.status_code == 204


class TestSendToNonexistentGroup:
    def test_send_to_nonexistent_group(self) -> None:
        app = _make_app()
        client = TestClient(app)

        resp = client.post(
            "/api/fanout/groups/does-not-exist/send",
            json={"data": "echo hello\n"},
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"].lower()
