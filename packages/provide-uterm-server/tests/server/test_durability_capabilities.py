#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Durability capability reporting for the FastAPI server."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.app.control_plane import _build_durability_capabilities


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


def test_sqlite_durability_advertises_only_the_wired_resume_token_store() -> None:
    cfg = SimpleNamespace(control_plane=SimpleNamespace(backend="sqlite"))
    caps = _build_durability_capabilities(cfg)
    # Only the resume-token store is wired into the reference server. Session
    # records are NOT written to the control plane (no ControlPlaneSessionStore),
    # so the advert must not claim them as durable.
    assert caps.durable_state == ("resume_tokens",)
    assert "control_plane_session_records" not in caps.durable_state
    assert "approvals" not in caps.durable_state
    assert "leases" not in caps.durable_state
    assert "approvals" in caps.process_local_state
    assert "leases" in caps.process_local_state
    assert "session_registry_runtime_state" in caps.process_local_state
