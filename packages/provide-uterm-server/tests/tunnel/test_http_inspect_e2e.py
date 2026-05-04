#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E tests for HTTP inspection tunnel flow."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from provide.terminal.server.app import create_server_app
from provide.terminal.server.models import ServerConfig
from provide.terminal.tunnel.protocol import CHANNEL_DATA, CHANNEL_HTTP, encode_control, encode_frame


@pytest.fixture
def e2e_client():
    config = ServerConfig(auth={"mode": "none"})
    return TestClient(create_server_app(config))


class TestHttpInspectE2E:
    def test_create_http_tunnel(self, e2e_client):
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "http", "display_name": "http-test"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["tunnel_type"] == "http"
        assert body["tunnel_id"].startswith("tunnel-")

    def test_http_tunnel_share_url_points_to_inspect_page(self, e2e_client):
        """F3: share_url for http tunnels must use /app/inspect/ not /app/session/."""
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "http"})
        assert resp.status_code == 200
        body = resp.json()
        assert "/app/inspect/" in body["share_url"]
        assert "token=" in body["share_url"]

    def test_terminal_tunnel_share_url_points_to_session_page(self, e2e_client):
        """F3: share_url for non-http tunnels uses /app/session/."""
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "terminal"})
        assert resp.status_code == 200
        body = resp.json()
        assert "/app/session/" in body["share_url"]

    def test_rotate_http_tunnel_share_url_uses_inspect(self, e2e_client):
        """Rotate tokens for an HTTP tunnel must return /app/inspect/ in share_url."""
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "http"})
        assert resp.status_code == 200
        tid = resp.json()["tunnel_id"]
        resp2 = e2e_client.post(f"/api/tunnels/{tid}/tokens/rotate")
        assert resp2.status_code == 200
        assert "/app/inspect/" in resp2.json()["share_url"]

    def test_short_share_redirect_http_tunnel_uses_inspect(self, e2e_client):
        """GET /s/{id} for an HTTP tunnel must redirect to /app/inspect/."""
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "http"})
        tid = resp.json()["tunnel_id"]
        token = resp.json()["share_url"].split("token=")[1]
        r = e2e_client.get(f"/s/{tid}?token={token}", follow_redirects=False)
        assert r.status_code == 302
        assert "/app/inspect/" in r.headers["location"]

    def test_short_share_redirect_unknown_tunnel_defaults_to_session(self, e2e_client):
        """GET /s/{unknown} falls back to /app/session/ when no token entry exists."""
        r = e2e_client.get("/s/no-such-tunnel", follow_redirects=False)
        assert r.status_code == 302
        assert "/app/session/" in r.headers["location"]

    def test_http_channel_frame_accepted(self, e2e_client):
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "http"})
        tid = resp.json()["tunnel_id"]
        with e2e_client.websocket_connect(f"/tunnel/{tid}") as ws:
            ws.send_bytes(encode_control({"type": "open", "channel": 3, "tunnel_type": "http"}))
            http_req = json.dumps(
                {
                    "type": "http_req",
                    "id": "r1",
                    "method": "GET",
                    "url": "/test",
                    "headers": {},
                    "body_size": 0,
                }
            ).encode()
            ws.send_bytes(encode_frame(CHANNEL_HTTP, http_req))
            http_res = json.dumps(
                {
                    "type": "http_res",
                    "id": "r1",
                    "status": 200,
                    "status_text": "OK",
                    "headers": {},
                    "body_size": 5,
                    "duration_ms": 42,
                }
            ).encode()
            ws.send_bytes(encode_frame(CHANNEL_HTTP, http_res))

    def test_terminal_and_http_channels_coexist(self, e2e_client):
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "http"})
        tid = resp.json()["tunnel_id"]
        with e2e_client.websocket_connect(f"/tunnel/{tid}") as ws:
            ws.send_bytes(encode_control({"type": "open", "channel": 3, "tunnel_type": "http"}))
            # Send terminal data on channel 1
            ws.send_bytes(encode_frame(CHANNEL_DATA, b"[log] request proxied\n"))
            # Send HTTP data on channel 3
            ws.send_bytes(
                encode_frame(
                    CHANNEL_HTTP,
                    json.dumps(
                        {
                            "type": "http_req",
                            "id": "r1",
                            "method": "GET",
                            "url": "/",
                            "headers": {},
                            "body_size": 0,
                        }
                    ).encode(),
                )
            )
            # Both channels accepted without error

    def test_multiple_http_exchanges(self, e2e_client):
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "http"})
        tid = resp.json()["tunnel_id"]
        with e2e_client.websocket_connect(f"/tunnel/{tid}") as ws:
            ws.send_bytes(encode_control({"type": "open", "channel": 3, "tunnel_type": "http"}))
            for i in range(5):
                req = json.dumps(
                    {
                        "type": "http_req",
                        "id": f"r{i}",
                        "method": "GET",
                        "url": f"/api/item/{i}",
                        "headers": {},
                        "body_size": 0,
                    }
                ).encode()
                ws.send_bytes(encode_frame(CHANNEL_HTTP, req))
                res = json.dumps(
                    {
                        "type": "http_res",
                        "id": f"r{i}",
                        "status": 200,
                        "status_text": "OK",
                        "headers": {},
                        "body_size": 10,
                        "duration_ms": 5.0 + i,
                    }
                ).encode()
                ws.send_bytes(encode_frame(CHANNEL_HTTP, res))

    def test_http_req_with_body(self, e2e_client):
        import base64

        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "http"})
        tid = resp.json()["tunnel_id"]
        with e2e_client.websocket_connect(f"/tunnel/{tid}") as ws:
            ws.send_bytes(encode_control({"type": "open", "channel": 3, "tunnel_type": "http"}))
            body_b64 = base64.b64encode(b'{"user":"admin"}').decode()
            req = json.dumps(
                {
                    "type": "http_req",
                    "id": "r1",
                    "method": "POST",
                    "url": "/api/login",
                    "headers": {"content-type": "application/json"},
                    "body_size": 17,
                    "body_b64": body_b64,
                }
            ).encode()
            ws.send_bytes(encode_frame(CHANNEL_HTTP, req))

    def test_invalid_json_on_http_channel(self, e2e_client):
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "http"})
        tid = resp.json()["tunnel_id"]
        with e2e_client.websocket_connect(f"/tunnel/{tid}") as ws:
            ws.send_bytes(encode_control({"type": "open", "channel": 3, "tunnel_type": "http"}))
            ws.send_bytes(encode_frame(CHANNEL_HTTP, b"not valid json"))
            # Should not crash — just logged as warning
