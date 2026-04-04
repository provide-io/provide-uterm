#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
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
