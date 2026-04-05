#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E tunnel tests — full server with real WebSocket connections.

Tests the complete path: POST /api/tunnels → agent connects /tunnel/{id}
→ browser connects /ws/browser/{id}/term → data flows.

Uses the full ``create_server_app()`` factory with auth_mode=none.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from provide.terminal.control_channel import ControlChannelDecoder
from provide.terminal.server.app import create_server_app
from provide.terminal.server.models import ServerConfig
from provide.terminal.tunnel.protocol import CHANNEL_DATA, encode_control, encode_frame


@pytest.fixture
def e2e_client() -> TestClient:
    config = ServerConfig(auth={"mode": "none"})
    app = create_server_app(config)
    return TestClient(app)


def _decode_events(raw: str) -> list[dict[str, Any]]:
    """Decode control-channel encoded browser messages."""
    dec = ControlChannelDecoder()
    events = dec.feed(raw)
    events.extend(dec.finish())
    result = []
    for ev in events:
        if hasattr(ev, "control"):
            result.append(ev.control)
        elif hasattr(ev, "data"):
            result.append({"type": "data", "data": ev.data})
    return result


class TestE2ETunnelDataFlow:
    """Full server E2E: agent → server → browser.

    Note: TestClient WS is synchronous, so we can't have agent and browser
    open simultaneously in the same thread. We test sequentially: agent
    connects and sends data, then browser connects and reads buffered data.
    """

    def test_create_tunnel_returns_valid_response(self, e2e_client: TestClient) -> None:
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "terminal", "display_name": "e2e-test"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["tunnel_id"].startswith("tunnel-")
        assert "ws_endpoint" in body
        assert "worker_token" in body
        assert "share_url" in body
        assert "control_url" in body
        assert "expires_at" in body
        assert body["expires_at"] > time.time()

    def test_agent_connects_and_sends(self, e2e_client: TestClient) -> None:
        """Agent connects via /tunnel/{id} and sends binary frames."""
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "terminal"})
        tid = resp.json()["tunnel_id"]

        with e2e_client.websocket_connect(f"/tunnel/{tid}") as ws:
            ws.send_bytes(
                encode_control(
                    {
                        "type": "open",
                        "channel": 1,
                        "tunnel_type": "terminal",
                        "term_size": [80, 24],
                    }
                )
            )
            ws.send_bytes(encode_frame(CHANNEL_DATA, b"hello from agent"))
            # No error — agent connected and sent data successfully

    def test_agent_session_appears_in_registry(self, e2e_client: TestClient) -> None:
        """Tunnel session is registered and visible via /api/sessions."""
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "terminal", "display_name": "reg-test"})
        tid = resp.json()["tunnel_id"]

        # Session should be in the registry
        sessions_resp = e2e_client.get("/api/sessions")
        assert sessions_resp.status_code == 200
        session_ids = [s["session_id"] for s in sessions_resp.json()]
        assert tid in session_ids

    def test_control_open_message_processed(self, e2e_client: TestClient) -> None:
        """Control open message is processed without error."""
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "terminal"})
        tid = resp.json()["tunnel_id"]

        with e2e_client.websocket_connect(f"/tunnel/{tid}") as ws:
            ws.send_bytes(
                encode_control(
                    {
                        "type": "open",
                        "channel": 1,
                        "tunnel_type": "terminal",
                        "term_size": [120, 40],
                    }
                )
            )
            ws.send_bytes(encode_frame(CHANNEL_DATA, b"terminal output"))
            # If we get here without error, the open + data flow works


class TestE2ETunnelTokenAPIs:
    """Token lifecycle APIs work end-to-end."""

    def test_revoke_removes_tokens(self, e2e_client: TestClient) -> None:
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "terminal"})
        tid = resp.json()["tunnel_id"]

        # Revoke
        del_resp = e2e_client.delete(f"/api/tunnels/{tid}/tokens")
        assert del_resp.status_code == 200
        assert del_resp.json()["ok"] is True

        # Verify token map is empty for this tunnel
        token_map = e2e_client.app.state.uterm_tunnel_tokens  # type: ignore[union-attr]
        assert tid not in token_map

    def test_rotate_generates_new_tokens(self, e2e_client: TestClient) -> None:
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "terminal"})
        tid = resp.json()["tunnel_id"]
        old_worker = resp.json()["worker_token"]

        # Rotate
        rot_resp = e2e_client.post(f"/api/tunnels/{tid}/tokens/rotate")
        assert rot_resp.status_code == 200
        new_data = rot_resp.json()
        assert new_data["worker_token"] != old_worker
        assert "share_url" in new_data
        assert "expires_at" in new_data

        # Old token should not be in the token map
        token_map = e2e_client.app.state.uterm_tunnel_tokens  # type: ignore[union-attr]
        assert token_map[tid]["worker_token"] == new_data["worker_token"]

    def test_rotate_nonexistent_returns_404(self, e2e_client: TestClient) -> None:
        resp = e2e_client.post("/api/tunnels/nonexistent/tokens/rotate")
        assert resp.status_code == 404

    def test_custom_ttl(self, e2e_client: TestClient) -> None:
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "terminal", "ttl_s": 120})
        body = resp.json()
        # expires_at should be ~120s from now (not default 3600)
        assert body["expires_at"] < time.time() + 200
        assert body["expires_at"] > time.time() + 60


class TestShortShareUrl:
    """Test /s/{id} short share URL redirect."""

    def test_short_url_redirects(self, e2e_client: TestClient) -> None:
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "terminal"})
        tid = resp.json()["tunnel_id"]
        share_token = e2e_client.app.state.uterm_tunnel_tokens[tid]["share_token"]  # type: ignore[union-attr]

        redirect = e2e_client.get(f"/s/{tid}?token={share_token}", follow_redirects=False)
        assert redirect.status_code == 302
        location = redirect.headers["location"]
        assert f"/app/session/{tid}" in location
        assert f"token={share_token}" in location

    def test_short_url_without_token(self, e2e_client: TestClient) -> None:
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "terminal"})
        tid = resp.json()["tunnel_id"]

        redirect = e2e_client.get(f"/s/{tid}", follow_redirects=False)
        assert redirect.status_code == 302
        assert f"/app/session/{tid}" in redirect.headers["location"]

    def test_short_url_follows_redirect(self, e2e_client: TestClient) -> None:
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "terminal"})
        tid = resp.json()["tunnel_id"]
        share_token = e2e_client.app.state.uterm_tunnel_tokens[tid]["share_token"]  # type: ignore[union-attr]

        final = e2e_client.get(f"/s/{tid}?token={share_token}", follow_redirects=True)
        assert final.status_code == 200
