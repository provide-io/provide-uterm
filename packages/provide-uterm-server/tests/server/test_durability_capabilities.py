#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Durability capability reporting for the FastAPI server."""

from __future__ import annotations

from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config


def test_sqlite_mode_reports_process_local_tunnel_webhook_and_fanout_state(tmp_path) -> None:
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    config.sessions = []
    config.control_plane.backend = "sqlite"
    config.control_plane.database_url = str(tmp_path / "control-plane.db")
    app = create_server_app(config)

    capabilities = app.state.uterm_durability_capabilities

    assert capabilities["control_plane_backend"] == "sqlite"
    assert capabilities["ha_safe"] is False
    assert "resume_tokens" in capabilities["durable_state"]
    assert "tunnel_tokens" in capabilities["process_local_state"]
    assert "webhook_registrations" in capabilities["process_local_state"]
    assert "fanout_groups" in capabilities["process_local_state"]

    with TestClient(app) as client:
        response = client.get("/api/durability/capabilities")

    assert response.status_code == 200
    assert response.json() == capabilities
