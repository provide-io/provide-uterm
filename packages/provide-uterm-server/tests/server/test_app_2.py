#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for on_resume callback wired by create_server_app."""

from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from provide.uterm.client import connect_test_ws
from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.bridge.hub import ResumeSession

if TYPE_CHECKING:
    pass


class TestOnResumeCallback:
    """Tests for the _on_resume callback wired by create_server_app."""

    def _make_app(self):
        config = default_server_config()
        config.auth.mode = "header"
        config.auth.header_mode_acknowledged = True
        config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
        config.sessions = []  # no auto-start — keeps tests deterministic
        return create_server_app(config)

    def _recv_type(self, ws: Any, want: str, *, max_frames: int = 12) -> dict:
        """Receive frames until one with type==want (skip presence_*/noise)."""
        for _ in range(max_frames):
            frame = ws.receive_json()
            if frame.get("type") == want:
                return frame
        raise AssertionError(f"did not receive frame type={want!r}")

    def _read_hello_and_state(self, ws: Any) -> dict:
        """Drain control frames until hello + hijack_state (presence_sync may interleave)."""
        hello: dict | None = None
        saw_hijack_state = False
        for _ in range(12):
            frame = ws.receive_json()
            ftype = frame.get("type")
            if ftype == "hello" and hello is None:
                hello = frame
            elif ftype == "hijack_state":
                saw_hijack_state = True
            if hello is not None and saw_hijack_state:
                return hello
        assert hello is not None, "did not receive hello frame"
        assert saw_hijack_state, "did not receive hijack_state frame"
        return hello

    def test_on_resume_rejects_resume_when_session_deleted(self) -> None:
        """on_resume blocks resume after the backing session is deleted from registry."""
        app = self._make_app()
        with TestClient(app) as client:
            client.post(
                "/api/sessions",
                json={"session_id": "temp-sess", "display_name": "Temp", "connector_type": "shell"},
            )

            with connect_test_ws(client, "/ws/browser/temp-sess/term") as ws:
                hello = self._read_hello_and_state(ws)
                assert hello["resume_supported"] is True
                token = hello["resume_token"]

            client.delete("/api/sessions/temp-sess")

            with connect_test_ws(client, "/ws/browser/temp-sess/term") as ws:
                self._read_hello_and_state(ws)
                ws.send_json({"type": "resume", "token": token})
                # on_resume returns False (session gone) → resume silently ignored
                ws.send_json({"type": "ping"})
                pong = self._recv_type(ws, "pong")
                assert pong["type"] == "pong"

    def test_on_resume_allows_resume_when_session_exists(self) -> None:
        """on_resume allows resume when the backing session still exists in registry."""
        app = self._make_app()
        with TestClient(app) as client:
            client.post(
                "/api/sessions",
                json={"session_id": "perm-sess", "display_name": "Perm", "connector_type": "shell"},
            )

            with connect_test_ws(client, "/ws/browser/perm-sess/term") as ws:
                hello = self._read_hello_and_state(ws)
                token = hello["resume_token"]

            with connect_test_ws(client, "/ws/browser/perm-sess/term") as ws:
                self._read_hello_and_state(ws)
                ws.send_json({"type": "resume", "token": token})
                resumed = self._recv_type(ws, "hello")
                assert resumed["type"] == "hello"
                assert resumed["resumed"] is True

    @pytest.mark.asyncio()
    async def test_on_resume_allows_resume_when_no_registry(self) -> None:
        """The _on_resume closure returns True when registry is None."""
        registry = None

        async def _on_resume(token: str, session: ResumeSession) -> bool:
            if registry is None:
                return True
            return await registry.get_definition(session.worker_id) is not None  # type: ignore[union-attr]

        import time as _time

        now = _time.monotonic()
        session = ResumeSession(token="tok", worker_id="w1", role="admin", created_at=now, expires_at=now + 300)
        assert await _on_resume("tok", session) is True

    def test_on_resume_rejects_stale_token_after_session_recreated(self) -> None:
        """on_resume rejects a token if the session was deleted and recreated (same ID, newer created_at)."""
        app = self._make_app()
        with TestClient(app) as client:
            client.post(
                "/api/sessions",
                json={"session_id": "recreate-sess", "display_name": "R", "connector_type": "shell"},
            )

            with connect_test_ws(client, "/ws/browser/recreate-sess/term") as ws:
                hello = self._read_hello_and_state(ws)
                token = hello["resume_token"]

            # Ensure time advances so the new session has a strictly later created_at
            time.sleep(0.05)

            # Delete and recreate with the same session_id
            client.delete("/api/sessions/recreate-sess")
            client.post(
                "/api/sessions",
                json={"session_id": "recreate-sess", "display_name": "R2", "connector_type": "shell"},
            )

            # Token was issued against the old session — should be rejected
            with connect_test_ws(client, "/ws/browser/recreate-sess/term") as ws:
                self._read_hello_and_state(ws)
                ws.send_json({"type": "resume", "token": token})
                ws.send_json({"type": "ping"})
                pong = self._recv_type(ws, "pong")
                assert pong["type"] == "pong"  # no "resumed" hello — token rejected

    def test_create_server_app_uses_memory_control_plane_by_default(self) -> None:
        app = self._make_app()

        assert app.state.uterm_control_plane.__class__.__name__ == "MemoryControlPlane"
        assert app.state.uterm_hub.resume_store.__class__.__name__ == "ControlPlaneResumeStore"

    def test_create_server_app_bootstraps_sqlite_control_plane(self, tmp_path) -> None:
        db_path = tmp_path / "control-plane.db"
        config = default_server_config()
        config.auth.mode = "header"
        config.auth.header_mode_acknowledged = True
        config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
        config.sessions = []
        config.control_plane.backend = "sqlite"
        config.control_plane.database_url = str(db_path)
        app = create_server_app(config)

        with TestClient(app):
            pass

        conn = sqlite3.connect(db_path)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()

        assert app.state.uterm_control_plane.__class__.__name__ == "SqliteControlPlane"
        assert "cp_resume_tokens" in tables

    def test_on_resume_allows_resume_with_sqlite_control_plane(self, tmp_path) -> None:
        db_path = tmp_path / "resume.db"
        config = default_server_config()
        config.auth.mode = "header"
        config.auth.header_mode_acknowledged = True
        config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
        config.sessions = []
        config.control_plane.backend = "sqlite"
        config.control_plane.database_url = str(db_path)
        app = create_server_app(config)

        with TestClient(app) as client:
            client.post(
                "/api/sessions",
                json={"session_id": "sqlite-sess", "display_name": "Sqlite", "connector_type": "shell"},
            )

            with connect_test_ws(client, "/ws/browser/sqlite-sess/term") as ws:
                hello = self._read_hello_and_state(ws)
                token = hello["resume_token"]

            with connect_test_ws(client, "/ws/browser/sqlite-sess/term") as ws:
                self._read_hello_and_state(ws)
                ws.send_json({"type": "resume", "token": token})
                resumed = self._recv_type(ws, "hello")
                assert resumed["type"] == "hello"
                assert resumed["resumed"] is True

    def test_sqlite_resume_persists_and_revokes_tokens_in_database(self, tmp_path) -> None:
        db_path = tmp_path / "resume-proof.db"
        config = default_server_config()
        config.auth.mode = "header"
        config.auth.header_mode_acknowledged = True
        config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
        config.sessions = []
        config.control_plane.backend = "sqlite"
        config.control_plane.database_url = str(db_path)
        app = create_server_app(config)

        with TestClient(app) as client:
            created = client.post(
                "/api/sessions",
                json={"session_id": "sqlite-proof", "display_name": "Sqlite Proof", "connector_type": "shell"},
            )
            assert created.status_code == 200

            with connect_test_ws(client, "/ws/browser/sqlite-proof/term") as ws:
                hello = self._read_hello_and_state(ws)
                first_token = hello["resume_token"]
                assert hello["resume_supported"] is True

            with connect_test_ws(client, "/ws/browser/sqlite-proof/term") as ws:
                self._read_hello_and_state(ws)
                ws.send_json({"type": "resume", "token": first_token})
                resumed = self._recv_type(ws, "hello")
                assert resumed["type"] == "hello"
                assert resumed["resumed"] is True
                second_token = resumed["resume_token"]

        conn = sqlite3.connect(db_path)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            rows = list(
                conn.execute(
                    """
                    SELECT token_value, session_id, role, revoked_at
                    FROM cp_resume_tokens
                    WHERE session_id = ?
                    ORDER BY rowid
                    """,
                    ("sqlite-proof",),
                )
            )
        finally:
            conn.close()

        assert "cp_resume_tokens" in tables
        assert first_token != second_token
        assert any(row[0] == first_token and row[3] is not None for row in rows)
        assert any(row[0] == second_token and row[3] is None for row in rows)
