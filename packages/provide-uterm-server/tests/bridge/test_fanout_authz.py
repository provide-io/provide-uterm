#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Integration tests for fan-out REST routes and authorization."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.bridge.fanout._controller import FanOutController
from provide.uterm.server.bridge.fanout._models import FanOutGroup
from provide.uterm.server.bridge.fanout._routes import _require_global_admin
from provide.uterm.server.bridge.identity import Principal
from provide.uterm.server.config_schema import SessionDefinition

VIEWER = {"X-Uterm-Principal": "bob", "X-Uterm-Role": "viewer"}
ADMIN = Principal(subject_id="admin", roles=frozenset({"admin"}))


def _authorized_controller(hub: Any, **overrides: Any) -> FanOutController:
    async def _is_admin(principal: Principal) -> bool:
        return "admin" in principal.roles and principal.admin_session_scope is None

    async def _resolve(worker_id: str) -> SessionDefinition:
        return _sess(worker_id, owner="admin", visibility="public")

    async def _can_read(principal: Principal, session: SessionDefinition) -> bool:
        return True

    kwargs = {
        "is_global_admin": _is_admin,
        "resolve_session": _resolve,
        "can_read_session": _can_read,
    }
    kwargs.update(overrides)
    return FanOutController(hub, **kwargs)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/fanout/groups"),
        ("GET", "/api/fanout/groups"),
        ("DELETE", "/api/fanout/groups/not-disclosed"),
        ("POST", "/api/fanout/groups/not-disclosed/send"),
        ("POST", "/api/fanout/groups/not-disclosed/grants"),
    ],
)
@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"X-Uterm-Principal": "anonymous", "X-Uterm-Role": "viewer"}, 401),
        (VIEWER, 403),
        ({"X-Uterm-Principal": "operator", "X-Uterm-Role": "operator"}, 403),
    ],
)
def test_fanout_routes_reject_non_admin_before_parsing_or_lookup(
    method: str,
    path: str,
    headers: dict[str, str],
    expected: int,
) -> None:
    app = _make_app(allow_unknown_members=True)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.request(method, path, content=b"{", headers=headers)

    assert response.status_code == expected


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/fanout/groups"),
        ("GET", "/api/fanout/groups"),
        ("DELETE", "/api/fanout/groups/not-disclosed"),
        ("POST", "/api/fanout/groups/not-disclosed/send"),
        ("POST", "/api/fanout/groups/not-disclosed/grants"),
    ],
)
def test_fanout_routes_reject_session_scoped_admin_before_parsing_or_lookup(method: str, path: str) -> None:
    app = _make_app(allow_unknown_members=True)
    app.state.uterm_authz = MagicMock(is_admin=AsyncMock(return_value=False))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.request(
        method,
        path,
        content=b"{",
        headers={"X-Uterm-Principal": "scoped-admin", "X-Uterm-Role": "admin"},
    )

    assert response.status_code == 403


def _make_app(
    sessions: list[SessionDefinition] | None = None,
    *,
    allow_unknown_members: bool = False,
) -> Any:
    cfg = default_server_config()
    cfg.auth.mode = "header"
    cfg.auth.header_mode_acknowledged = True
    cfg.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    cfg.fanout_allow_unknown_members = allow_unknown_members
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
        app = _make_app(allow_unknown_members=True)
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
        assert result["error"] is None
        assert result["approval_required"] is False
        assert result["approval_id"] is None

        # Delete group
        resp = client.delete(f"/api/fanout/groups/{group_id}")
        assert resp.status_code == 204

        # Confirm deletion
        resp = client.get("/api/fanout/groups")
        assert resp.status_code == 200
        assert not any(g["group_id"] == group_id for g in resp.json())


@pytest.mark.parametrize(
    ("action", "error", "approval_required"),
    [("deny", "blocked", False), ("hold", None, True)],
)
def test_send_route_keeps_http_200_for_policy_results(
    action: str,
    error: str | None,
    approval_required: bool,
) -> None:
    app = _make_app(allow_unknown_members=True)
    gate = MagicMock()
    gate.intercept_fanout = AsyncMock(
        return_value=MagicMock(action=action, reason="blocked" if action == "deny" else None)
    )
    app.state.uterm_hub.fan_out_controller._fanout_policy_gate = gate
    client = TestClient(app)
    created = client.post("/api/fanout/groups", json={"name": "policy", "worker_ids": ["w1"]})
    group_id = created.json()["group_id"]

    response = client.post(f"/api/fanout/groups/{group_id}/send", json={"data": "id"})

    assert response.status_code == 200
    assert response.json()["error"] == error
    assert response.json()["approval_required"] is approval_required


class TestCreateGroupExceedsMaxSize:
    def test_create_group_exceeds_max_size(self) -> None:
        app = _make_app(allow_unknown_members=True)
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
        app = _make_app(allow_unknown_members=True)
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
    def test_rejects_unknown_session_by_default(self) -> None:
        app = _make_app()
        client = TestClient(app)

        resp = client.post(
            "/api/fanout/groups",
            json={"name": "g", "worker_ids": ["future-worker"]},
        )

        assert resp.status_code == 400
        assert "future-worker" in resp.json()["error"]

    def test_explicitly_allows_dormant_unknown_session(self) -> None:
        app = _make_app(allow_unknown_members=True)
        client = TestClient(app)

        resp = client.post(
            "/api/fanout/groups",
            json={"name": "g", "worker_ids": ["future-worker"]},
        )

        assert resp.status_code == 200

    def test_rejects_session_principal_cannot_read(self) -> None:
        # Fan-out rejects viewers before disclosing private-session membership.
        app = _make_app([_sess("priv1", owner="alice", visibility="private")])
        client = TestClient(app)
        resp = client.post(
            "/api/fanout/groups",
            json={"name": "g", "worker_ids": ["priv1"]},
            headers=VIEWER,
        )
        assert resp.status_code == 403
        assert "admin" in resp.json()["detail"]

    def test_allows_readable_session(self) -> None:
        # Public visibility does not let a viewer create a global fan-out group.
        app = _make_app([_sess("pub1", owner="alice", visibility="public")])
        client = TestClient(app)
        resp = client.post(
            "/api/fanout/groups",
            json={"name": "g", "worker_ids": ["pub1"]},
            headers=VIEWER,
        )
        assert resp.status_code == 403

    def test_global_admin_creates_group_for_known_readable_session(self) -> None:
        app = _make_app([_sess("pub1", owner="admin", visibility="public")])
        client = TestClient(app)

        response = client.post(
            "/api/fanout/groups",
            json={"name": "known", "worker_ids": ["pub1"]},
        )

        assert response.status_code == 200
        assert client.get("/api/fanout/groups").json()[0]["session_count"] == 1

    @pytest.mark.parametrize("missing", ["resolve", "read"])
    def test_default_reject_missing_controller_authorizer_dependency_creates_no_group(self, missing: str) -> None:
        app = _make_app([_sess("pub1", owner="admin", visibility="public")])
        controller = app.state.uterm_hub.fan_out_controller
        setattr(controller, {"resolve": "_resolve_session", "read": "_can_read_session"}[missing], None)
        client = TestClient(app)

        response = client.post(
            "/api/fanout/groups",
            json={"name": "must-not-exist", "worker_ids": ["pub1"]},
        )

        assert response.status_code == 403
        assert client.get("/api/fanout/groups").json() == []


async def test_route_admin_requirement_rejects_missing_principal() -> None:
    request = MagicMock()
    request.state = MagicMock(spec=[])

    with pytest.raises(HTTPException) as error:
        await _require_global_admin(request)

    assert error.value.status_code == 401


async def test_route_admin_requirement_fails_closed_when_authorizer_raises() -> None:
    request = MagicMock()
    request.state = MagicMock(uterm_principal=ADMIN)
    request.app.state.uterm_authz.is_admin = AsyncMock(side_effect=RuntimeError("authz unavailable"))

    with pytest.raises(HTTPException) as error:
        await _require_global_admin(request)

    assert error.value.status_code == 403


async def test_validate_members_reports_readable_member_as_allowed() -> None:
    controller = _authorized_controller(MagicMock())

    allowed, refused = await controller.validate_members(["w1"], ADMIN)

    assert allowed == ["w1"]
    assert refused == []


async def test_direct_send_fails_closed_when_global_admin_check_raises() -> None:
    hub = MagicMock(broadcast=AsyncMock(), send_worker=AsyncMock())
    hub.approval_store = None
    controller = _authorized_controller(hub, is_global_admin=AsyncMock(side_effect=RuntimeError("down")))

    result = await controller.send("missing", "id", principal=ADMIN)

    assert result.error == "fan-out authorization failed"
    hub.send_worker.assert_not_awaited()


async def test_direct_send_reports_unknown_group_after_successful_admin_check() -> None:
    hub = MagicMock(broadcast=AsyncMock(), send_worker=AsyncMock())
    hub.approval_store = None
    controller = _authorized_controller(hub)

    result = await controller.send("missing", "id", principal=ADMIN)

    assert result.error == "fan-out group not found"


async def test_direct_send_fails_member_when_session_resolution_raises() -> None:
    hub = MagicMock(broadcast=AsyncMock(), send_worker=AsyncMock())
    hub.approval_store = None
    controller = _authorized_controller(hub, resolve_session=AsyncMock(side_effect=RuntimeError("registry down")))
    await controller._store.save(
        FanOutGroup(group_id="g", name="G", worker_ids=["w1"], created_by="admin", created_at=0.0)
    )

    result = await controller.send("g", "id", principal=ADMIN)

    assert result.failed_sessions == ["w1"]
    hub.send_worker.assert_not_awaited()


def test_unrecognized_policy_role_normalizes_to_viewer() -> None:
    principal = Principal(subject_id="custom", roles=frozenset({"custom"}))

    assert FanOutController._strongest_role(principal) == "viewer"


class TestFanOutObserverTransparency:
    async def test_send_broadcasts_fanout_input_to_each_target(self) -> None:
        """Each target session's observers get a fanout_input frame carrying the
        originating principal, so they can distinguish it from a local hijack."""
        hub = MagicMock()
        hub.broadcast = AsyncMock()
        hub.send_worker = AsyncMock(return_value=False)  # no workers connected
        hub.approval_store = None
        ctrl = _authorized_controller(hub, fanout_policy_gate=None)
        group = FanOutGroup(group_id="g1", name="g", worker_ids=["wa", "wb"], created_by="alice", created_at=0.0)
        await ctrl._store.save(group)

        await ctrl.send(
            "g1",
            "uptime\n",
            principal=Principal(subject_id="alice", roles=frozenset({"admin"})),
        )

        sent = {call.args[0]: call.args[1] for call in hub.broadcast.await_args_list}
        assert set(sent) == {"wa", "wb"}
        for frame in sent.values():
            assert frame["type"] == "fanout_input"
            assert frame["from_principal"] == "alice"
            assert frame["command"] == "uptime\n"
            assert frame["group_id"] == "g1"


@pytest.mark.asyncio
async def test_send_rechecks_current_session_authorization() -> None:
    hub = MagicMock()
    hub.broadcast = AsyncMock()
    hub.send_worker = AsyncMock(return_value=True)
    hub.approval_store = None
    definition = _sess("w1", owner="alice", visibility="private")
    readable = True

    async def resolve_session(worker_id: str) -> SessionDefinition | None:
        return definition if worker_id == "w1" else None

    async def can_read_session(principal: Principal, session: SessionDefinition) -> bool:
        assert session is definition
        return readable

    ctrl = FanOutController(
        hub,
        is_global_admin=AsyncMock(return_value=True),
        resolve_session=resolve_session,
        can_read_session=can_read_session,
    )
    group = FanOutGroup(group_id="g1", name="g", worker_ids=["w1"], created_by="alice", created_at=0.0)
    await ctrl.create_group(group, principal="alice")

    readable = False
    result = await ctrl.send(
        "g1",
        "whoami\n",
        principal=Principal(subject_id="alice", roles=frozenset({"admin"})),
    )

    hub.send_worker.assert_not_awaited()
    assert result.failed_sessions == ["w1"]
    assert result.results[0].worker_id == "w1"
    assert result.results[0].ok is False


@pytest.mark.asyncio
async def test_group_grant_does_not_bypass_session_authorization() -> None:
    hub = MagicMock()
    hub.broadcast = AsyncMock()
    hub.send_worker = AsyncMock(return_value=True)
    hub.approval_store = None
    definition = _sess("w1", owner="alice", visibility="private")

    ctrl = FanOutController(
        hub,
        is_global_admin=AsyncMock(return_value=True),
        resolve_session=AsyncMock(return_value=definition),
        can_read_session=AsyncMock(return_value=False),
    )
    group = FanOutGroup(
        group_id="g1",
        name="g",
        worker_ids=["w1"],
        created_by="alice",
        created_at=0.0,
        grants=["bob"],
    )
    await ctrl.create_group(group, principal="alice")

    result = await ctrl.send(
        "g1",
        "whoami\n",
        principal=Principal(subject_id="bob", roles=frozenset({"admin"})),
    )

    hub.send_worker.assert_not_awaited()
    hub.broadcast.assert_not_awaited()
    assert result.failed_sessions == ["w1"]


@pytest.mark.asyncio
async def test_direct_admin_cannot_send_to_a_guessed_group_without_group_acl() -> None:
    hub = MagicMock()
    hub.broadcast = AsyncMock()
    hub.send_worker = AsyncMock(return_value=True)
    hub.approval_store = None
    ctrl = _authorized_controller(hub)
    await ctrl._store.save(
        FanOutGroup(group_id="other-group", name="G", worker_ids=["w1"], created_by="alice", created_at=0.0)
    )

    result = await ctrl.send(
        "other-group",
        "id\n",
        principal=Principal(subject_id="mallory", roles=frozenset({"admin"})),
    )

    assert result.error == "fan-out group not found"
    hub.broadcast.assert_not_awaited()
    hub.send_worker.assert_not_awaited()


@pytest.mark.parametrize(
    "principal",
    [None, "admin", Principal(subject_id="viewer", roles=frozenset({"viewer"}))],
)
async def test_direct_send_rejects_missing_string_and_non_admin_principals(principal: Any) -> None:
    hub = MagicMock()
    hub.broadcast = AsyncMock()
    hub.send_worker = AsyncMock(return_value=True)
    hub.approval_store = None
    ctrl = _authorized_controller(hub)
    await ctrl._store.save(FanOutGroup(group_id="g", name="G", worker_ids=["w1"], created_by="admin", created_at=0.0))

    result = await ctrl.send("g", "id\n", principal=principal)

    assert result.error
    hub.broadcast.assert_not_awaited()
    hub.send_worker.assert_not_awaited()


@pytest.mark.parametrize("missing", ["admin", "resolve", "read"])
async def test_direct_send_fails_closed_when_authorizer_dependency_is_missing(missing: str) -> None:
    hub = MagicMock()
    hub.broadcast = AsyncMock()
    hub.send_worker = AsyncMock(return_value=True)
    hub.approval_store = None
    kwargs: dict[str, Any] = {
        "is_global_admin": AsyncMock(return_value=True),
        "resolve_session": AsyncMock(return_value=_sess("w1", owner="admin", visibility="public")),
        "can_read_session": AsyncMock(return_value=True),
    }
    kwargs[{"admin": "is_global_admin", "resolve": "resolve_session", "read": "can_read_session"}[missing]] = None
    ctrl = FanOutController(hub, **kwargs)
    await ctrl._store.save(FanOutGroup(group_id="g", name="G", worker_ids=["w1"], created_by="admin", created_at=0.0))

    result = await ctrl.send("g", "id\n", principal=ADMIN)

    assert result.error == "fan-out authorization is unavailable"
    hub.broadcast.assert_not_awaited()
    hub.send_worker.assert_not_awaited()


async def test_policy_context_uses_actual_strongest_role() -> None:
    hub = MagicMock()
    hub.broadcast = AsyncMock()
    hub.send_worker = AsyncMock(return_value=False)
    hub.approval_store = None
    gate = AsyncMock()
    gate.intercept_fanout = AsyncMock(return_value=MagicMock(action="deny", reason="blocked"))
    ctrl = _authorized_controller(hub, fanout_policy_gate=gate, is_global_admin=AsyncMock(return_value=True))
    await ctrl._store.save(
        FanOutGroup(group_id="g", name="G", worker_ids=["w1"], created_by="operator", created_at=0.0)
    )
    principal = Principal(subject_id="operator", roles=frozenset({"operator", "viewer"}))

    await ctrl.send("g", "id\n", principal=principal)

    context = gate.intercept_fanout.await_args.args[1]
    assert context.role == "operator"


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
