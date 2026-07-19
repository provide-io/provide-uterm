#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Full-app wiring + gating tests for the ``/gui/`` routes.

Proves the routes are mounted on the real server app behind the hub authz
dependency: ``gui/attach`` + the input routes gate on ``session.control.hijack``
(admin), ``gui/screenshot`` on ``session.read`` — mirroring the hijack routes.
Handler mechanics + every non-role-reachable branch are covered by the unit
suites (``bridge/test_rest_gui.py``, ``server/test_gui_session.py``).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.config_schema import GraphicalTargetConfig
from provide.uterm.server.config_schema_session import SessionDefinition

ADMIN = {"x-uterm-principal": "u1", "x-uterm-role": "admin", "x-uterm-tenant": "acme"}
VIEWER = {"x-uterm-principal": "u2", "x-uterm-role": "viewer", "x-uterm-tenant": "acme"}
WID = "gui-worker"
HID = "00000000-0000-0000-0000-000000000000"


def _client() -> TestClient:
    cfg = default_server_config()
    cfg.auth.mode = "header"
    cfg.auth.header_mode_acknowledged = True
    cfg.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    cfg.graphical_targets = [
        GraphicalTargetConfig(target_id="gt-mem", tenant_id="acme", protocol="memory", name="Mem", enabled=True),
    ]
    return TestClient(create_server_app(cfg))


def _register_session(client: TestClient, *, owner: str = "u1") -> None:
    client.app.state.uterm_registry._sessions[WID] = SessionDefinition(
        session_id=WID, connector_type="shell", visibility="public", owner=owner
    )


class TestAttachIntegration:
    def test_admin_attach_memory_ok(self) -> None:
        client = _client()
        _register_session(client)
        resp = client.post(f"/worker/{WID}/gui/attach", json={"target_id": "gt-mem"}, headers=ADMIN)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "target_id": "gt-mem"}
        # The graphical session was stored on the worker registry state.
        st = client.app.state.uterm_hub.registry.get(WID)
        assert st is not None
        assert st.graphical_session is not None

    def test_attach_unknown_session_404(self) -> None:
        # No registered session → the hub authz dependency rejects before the handler.
        resp = _client().post(f"/worker/{WID}/gui/attach", json={"target_id": "gt-mem"}, headers=ADMIN)
        assert resp.status_code == 404

    def test_attach_viewer_denied_hijack_capability(self) -> None:
        client = _client()
        _register_session(client)
        resp = client.post(f"/worker/{WID}/gui/attach", json={"target_id": "gt-mem"}, headers=VIEWER)
        assert resp.status_code == 403

    def test_attach_missing_target_id_422(self) -> None:
        client = _client()
        _register_session(client)
        resp = client.post(f"/worker/{WID}/gui/attach", json={}, headers=ADMIN)
        assert resp.status_code == 422


class TestGatingIntegration:
    def test_screenshot_read_gated_reaches_handler(self) -> None:
        # Admin passes session.read; with no active hijack the handler 404s.
        client = _client()
        _register_session(client)
        resp = client.get(f"/worker/{WID}/hijack/{HID}/gui/screenshot", headers=ADMIN)
        assert resp.status_code == 404
        assert resp.json()["error"] == "Invalid or expired hijack session."

    def test_click_hijack_gated_viewer_denied(self) -> None:
        client = _client()
        _register_session(client)
        resp = client.post(f"/worker/{WID}/hijack/{HID}/gui/click", json={"x": 1, "y": 1}, headers=VIEWER)
        assert resp.status_code == 403

    def test_screenshot_viewer_reaches_handler(self) -> None:
        # Viewer has session.read on a public session → past the dependency, 404.
        client = _client()
        _register_session(client)
        resp = client.get(f"/worker/{WID}/hijack/{HID}/gui/screenshot", headers=VIEWER)
        assert resp.status_code == 404
