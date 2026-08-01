#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import time
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.bridge.hub.approvals import ApprovalRequest, ApprovalStatus

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}
VIEWER_H = {"X-Uterm-Principal": "viewer-user", "X-Uterm-Role": "viewer"}


@pytest.fixture
def client():
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    app = create_server_app(config, api_only=True)
    return TestClient(app)


def test_list_approvals_empty(client):
    response = client.get("/api/approvals", headers=ADMIN_H)
    assert response.status_code == 200
    assert response.json() == []


def test_list_approvals_with_request(client):
    hub = client.app.state.uterm_hub
    req_id = str(uuid.uuid4())
    req = ApprovalRequest(
        id=req_id,
        worker_id="worker1",
        submitter_id="user1",
        command="ls -la",
        status=ApprovalStatus.PENDING,
        created_at=time.time(),
        expires_at=time.time() + 60,
    )
    hub.approval_store.add(req)

    response = client.get("/api/approvals", headers=ADMIN_H)
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["id"] == req_id
    assert data[0]["command"] == "ls -la"


def test_list_approvals_requires_admin(client):
    response = client.get("/api/approvals", headers=VIEWER_H)
    assert response.status_code == 403


def test_approve_request(client):
    hub = client.app.state.uterm_hub
    hub.resolve_approval = AsyncMock(return_value=(True, None))
    req_id = str(uuid.uuid4())
    req = ApprovalRequest(
        id=req_id,
        worker_id="worker1",
        submitter_id="user1",
        command="rm -rf /",
        status=ApprovalStatus.PENDING,
        created_at=time.time(),
        expires_at=time.time() + 60,
    )
    hub.approval_store.add(req)

    response = client.post(f"/api/approvals/{req_id}/approve", headers=ADMIN_H)
    assert response.status_code == 200
    assert response.json() == {"status": "approved"}

    updated_req = hub.approval_store.get(req_id)
    assert updated_req.status == ApprovalStatus.APPROVED


def test_approve_refuses_truthfully_when_delivery_owner_is_stale(client):
    hub = client.app.state.uterm_hub
    hub.resolve_approval = AsyncMock(return_value=(False, "invalid_owner"))
    req_id = str(uuid.uuid4())
    req = ApprovalRequest(
        id=req_id,
        worker_id="worker1",
        submitter_id="user1",
        command="rm -rf /",
        status=ApprovalStatus.PENDING,
        created_at=time.time(),
        expires_at=time.time() + 60,
    )
    hub.approval_store.add(req)

    response = client.post(f"/api/approvals/{req_id}/approve", headers=ADMIN_H)
    assert response.status_code == 409
    assert response.json()["detail"] == "Approval delivery refused: invalid_owner"
    assert hub.approval_store.get(req_id).status == ApprovalStatus.REFUSED


def test_reject_request(client):
    hub = client.app.state.uterm_hub
    req_id = str(uuid.uuid4())
    req = ApprovalRequest(
        id=req_id,
        worker_id="worker1",
        submitter_id="user1",
        command="format c:",
        status=ApprovalStatus.PENDING,
        created_at=time.time(),
        expires_at=time.time() + 60,
    )
    hub.approval_store.add(req)

    response = client.post(f"/api/approvals/{req_id}/reject", headers=ADMIN_H)
    assert response.status_code == 200
    assert response.json() == {"status": "rejected"}

    updated_req = hub.approval_store.get(req_id)
    assert updated_req.status == ApprovalStatus.REJECTED


def test_approve_own_command_rejected(client):
    """require_different_user: an admin cannot approve a command they submitted."""
    hub = client.app.state.uterm_hub
    req_id = str(uuid.uuid4())
    req = ApprovalRequest(
        id=req_id,
        worker_id="worker1",
        submitter_id="admin-user",  # same principal as ADMIN_H → self-approval
        command="rm -rf /",
        status=ApprovalStatus.PENDING,
        created_at=time.time(),
        expires_at=time.time() + 60,
    )
    hub.approval_store.add(req)

    response = client.post(f"/api/approvals/{req_id}/approve", headers=ADMIN_H)
    assert response.status_code == 403
    assert response.json()["detail"] == "Cannot approve your own command"
    # Must remain pending — not approved.
    assert hub.approval_store.get(req_id).status == ApprovalStatus.PENDING


def test_approve_not_found(client):
    response = client.post("/api/approvals/non-existent/approve", headers=ADMIN_H)
    assert response.status_code == 404


def test_approve_already_resolved_returns_400(client):
    # A request already in a terminal (non-PENDING) state can no longer be
    # claimed, so re-approving it must fail closed with HTTP 400 rather than
    # re-injecting the command.
    hub = client.app.state.uterm_hub
    req_id = str(uuid.uuid4())
    req = ApprovalRequest(
        id=req_id,
        worker_id="worker1",
        submitter_id="user1",
        command="rm -rf /",
        status=ApprovalStatus.APPROVED,
        created_at=time.time(),
        expires_at=time.time() + 60,
    )
    hub.approval_store.add(req)

    response = client.post(f"/api/approvals/{req_id}/approve", headers=ADMIN_H)
    assert response.status_code == 400
    assert "not pending" in response.json()["detail"]


def test_reject_already_resolved_returns_400(client):
    # Same fail-closed guarantee for the reject route: a request that has
    # already been rejected cannot be claimed again and must return HTTP 400.
    hub = client.app.state.uterm_hub
    req_id = str(uuid.uuid4())
    req = ApprovalRequest(
        id=req_id,
        worker_id="worker1",
        submitter_id="user1",
        command="format c:",
        status=ApprovalStatus.REJECTED,
        created_at=time.time(),
        expires_at=time.time() + 60,
    )
    hub.approval_store.add(req)

    response = client.post(f"/api/approvals/{req_id}/reject", headers=ADMIN_H)
    assert response.status_code == 400
    assert "not pending" in response.json()["detail"]
