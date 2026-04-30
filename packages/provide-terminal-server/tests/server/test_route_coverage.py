import pytest
from fastapi.testclient import TestClient
from provide.terminal.server import create_server_app, default_server_config


@pytest.fixture
def client():
    config = default_server_config()
    app = create_server_app(config)
    return TestClient(app)


def test_api_metrics_route(client):
    response = client.get("/api/metrics")
    assert response.status_code == 200
    assert "metrics" in response.json()


def test_api_sessions_list(client):
    response = client.get("/api/sessions")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_api_health_route(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["status"] == "ok"
    assert data["service"] == "uterm-server"
    assert "version" in data
    assert "uptime_s" in data
    assert "active_sessions" in data
    assert "control_plane_backend" in data


def test_healthz_route(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_static_assets_mount(client):
    response = client.get("/_terminal/terminal.js")
    assert response.status_code in (200, 404)


def test_app_redirect(client):
    response = client.get("/app", follow_redirects=False)
    assert response.status_code in (200, 307)


def test_rest_hijack_acquire_404(client):
    # Try to acquire a non-existent worker
    response = client.post("/worker/nonexistent/hijack/acquire", json={"owner": "test", "lease_s": 60})
    assert response.status_code == 404


def test_rest_hijack_heartbeat_404(client):
    response = client.post("/worker/nonexistent/hijack/h1/heartbeat", json={"lease_s": 60})
    assert response.status_code == 404
