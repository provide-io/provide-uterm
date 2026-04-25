#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import pytest
import time
import uuid
from fastapi.testclient import TestClient
from provide.terminal.server import create_server_app, default_server_config
from provide.terminal.bridge.hub.approvals import ApprovalRequest, ApprovalStatus

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}
VIEWER_H = {"X-Uterm-Principal": "viewer-user", "X-Uterm-Role": "viewer"}

@pytest.fixture
def client():
    config = default_server_config()
    config.auth.mode = "dev"
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
        expires_at=time.time() + 60
    )
    hub._approval_store.add(req)
    
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
    req_id = str(uuid.uuid4())
    req = ApprovalRequest(
        id=req_id,
        worker_id="worker1",
        submitter_id="user1",
        command="rm -rf /",
        status=ApprovalStatus.PENDING,
        created_at=time.time(),
        expires_at=time.time() + 60
    )
    hub._approval_store.add(req)
    
    response = client.post(f"/api/approvals/{req_id}/approve", headers=ADMIN_H)
    assert response.status_code == 200
    assert response.json() == {"status": "approved"}
    
    updated_req = hub._approval_store.get(req_id)
    assert updated_req.status == ApprovalStatus.APPROVED

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
        expires_at=time.time() + 60
    )
    hub._approval_store.add(req)
    
    response = client.post(f"/api/approvals/{req_id}/reject", headers=ADMIN_H)
    assert response.status_code == 200
    assert response.json() == {"status": "rejected"}
    
    updated_req = hub._approval_store.get(req_id)
    assert updated_req.status == ApprovalStatus.REJECTED

def test_approve_not_found(client):
    response = client.post("/api/approvals/non-existent/approve", headers=ADMIN_H)
    assert response.status_code == 404
