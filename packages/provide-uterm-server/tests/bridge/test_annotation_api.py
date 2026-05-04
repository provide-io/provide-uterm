#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Integration tests for POST /api/sessions/{session_id}/annotate."""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from provide.terminal.server import create_server_app, default_server_config


def _make_app() -> TestClient:
    cfg = default_server_config()
    cfg.auth.mode = "dev"
    return TestClient(create_server_app(cfg))


def test_annotate_session() -> None:
    """Annotating the default provide-shell session returns 200 with a ts."""
    client = _make_app()
    with client:
        # Start the runtime so get_runtime returns non-None
        client.post("/api/sessions/provide-shell/connect")
        r = client.post(
            "/api/sessions/provide-shell/annotate",
            json={"label": "test-label", "description": "a note", "severity": "info"},
        )
    assert r.status_code == 200
    body = r.json()
    assert "ts" in body
    assert isinstance(body["ts"], float)


def test_annotate_nonexistent_session() -> None:
    """Annotating a session that doesn't exist returns 404."""
    client = _make_app()
    with client:
        r = client.post(
            "/api/sessions/ghost-session/annotate",
            json={"label": "nope", "description": "", "severity": "info"},
        )
    assert r.status_code == 404


def test_annotate_no_active_runtime_returns_404() -> None:
    """Annotating a session that exists in registry but has no runtime yet returns 404."""
    cfg = default_server_config()
    cfg.auth.mode = "dev"
    # Add a never-started session
    from provide.terminal.server.models import SessionDefinition

    cfg.sessions = [
        SessionDefinition(
            session_id="no-runtime-sess",
            display_name="Never Started",
            connector_type="shell",
            auto_start=False,
        )
    ]
    with TestClient(create_server_app(cfg)) as client:
        # Session exists but no runtime was ever created (never connected)
        r = client.post(
            "/api/sessions/no-runtime-sess/annotate",
            json={"label": "stale", "description": "", "severity": "warning"},
        )
    assert r.status_code == 404


def test_annotation_appears_in_recording_entries() -> None:
    """Annotating a session with recording enabled stores the event in the recording."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = default_server_config()
        cfg.auth.mode = "dev"
        cfg.recording.enabled_by_default = True
        cfg.recording.directory = Path(tmpdir)  # type: ignore[assignment]

        with TestClient(create_server_app(cfg)) as client:
            # Connect so the runtime (and logger) is started
            client.post("/api/sessions/provide-shell/connect")

            # Annotate the session
            r = client.post(
                "/api/sessions/provide-shell/annotate",
                json={"label": "deploy-v1.2", "description": "deployment started", "severity": "info"},
            )
            assert r.status_code == 200

            # Retrieve recording entries filtered by annotation event type
            r2 = client.get("/api/sessions/provide-shell/recording/entries?event=annotation")
            assert r2.status_code == 200
            entries = r2.json()
            annotation_entries = [e for e in entries if e.get("event") == "annotation"]
            assert len(annotation_entries) >= 1
            data = annotation_entries[0].get("data", {})
            assert data.get("label") == "deploy-v1.2"
            assert data.get("source") == "agent"
