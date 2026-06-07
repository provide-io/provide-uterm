#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Integration tests for fan-out REST routes and authorization."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.config_schema import SessionDefinition

VIEWER = {"X-Uterm-Principal": "bob", "X-Uterm-Role": "viewer"}


def _make_app(sessions: list[SessionDefinition] | None = None) -> Any:
    cfg = default_server_config()
    cfg.auth.mode = "header"
    cfg.auth.header_mode_acknowledged = True
    cfg.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    cfg.sessions = sessions or []
    return create_server_app(cfg)


def _sess(session_id: str, *, owner: str, visibility: str) -> SessionDefinition:
    return SessionDefinition(
        session_id=session_id,
        display_name="T",
        connector_type="shell",
        owner=owner,
        visibility=visibility,  # type: ignore[arg-type]
    )


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


class TestCreateGroupReadAuthz:
    def test_rejects_session_principal_cannot_read(self) -> None:
        # A private session owned by alice; a viewer (bob) cannot read it → 403.
        app = _make_app([_sess("priv1", owner="alice", visibility="private")])
        client = TestClient(app)
        resp = client.post(
            "/api/fanout/groups",
            json={"name": "g", "worker_ids": ["priv1"]},
            headers=VIEWER,
        )
        assert resp.status_code == 403
        assert "priv1" in resp.json()["error"]

    def test_allows_readable_session(self) -> None:
        # A public session is readable by anyone → group creation succeeds.
        app = _make_app([_sess("pub1", owner="alice", visibility="public")])
        client = TestClient(app)
        resp = client.post(
            "/api/fanout/groups",
            json={"name": "g", "worker_ids": ["pub1"]},
            headers=VIEWER,
        )
        assert resp.status_code == 200


class TestFanOutObserverTransparency:
    async def test_send_broadcasts_fanout_input_to_each_target(self) -> None:
        """Each target session's observers get a fanout_input frame carrying the
        originating principal, so they can distinguish it from a local hijack."""
        from provide.uterm.server.bridge.fanout._controller import FanOutController
        from provide.uterm.server.bridge.fanout._models import FanOutGroup

        hub = MagicMock()
        hub.broadcast = AsyncMock()
        hub.send_worker = AsyncMock(return_value=False)  # no workers connected
        hub.approval_store = None
        ctrl = FanOutController(hub, fanout_policy_gate=None)
        group = FanOutGroup(group_id="g1", name="g", worker_ids=["wa", "wb"], created_by="alice", created_at=0.0)
        await ctrl._store.save(group)

        await ctrl.send("g1", "uptime\n", principal="alice")

        sent = {call.args[0]: call.args[1] for call in hub.broadcast.await_args_list}
        assert set(sent) == {"wa", "wb"}
        for frame in sent.values():
            assert frame["type"] == "fanout_input"
            assert frame["from_principal"] == "alice"
            assert frame["command"] == "uptime\n"
            assert frame["group_id"] == "g1"


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
