#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for on_resume callback wired by create_server_app."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from provide.terminal.bridge.hub import ResumeSession
from provide.terminal.client import connect_test_ws
from provide.terminal.server import create_server_app, default_server_config

if TYPE_CHECKING:
    pass


class TestOnResumeCallback:
    """Tests for the _on_resume callback wired by create_server_app."""

    def _make_app(self):
        config = default_server_config()
        config.auth.mode = "dev"
        config.sessions = []  # no auto-start — keeps tests deterministic
        return create_server_app(config)

    def _read_hello_and_state(self, ws: Any) -> dict:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        hs = ws.receive_json()
        assert hs["type"] == "hijack_state"
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
                pong = ws.receive_json()
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
                resumed = ws.receive_json()
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
                pong = ws.receive_json()
                assert pong["type"] == "pong"  # no "resumed" hello — token rejected
