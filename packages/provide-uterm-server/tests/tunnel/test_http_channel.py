#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for channel 0x03 HTTP inspection frame handling."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.tunnel.fastapi_routes import register_tunnel_routes as _tunnel_registrar
from provide.uterm.tunnel.protocol import CHANNEL_DATA, CHANNEL_HTTP, encode_frame


@pytest.fixture
def client():
    hub = TermHub()
    app = FastAPI()
    app.include_router(hub.create_router(extra_route_registrars=[_tunnel_registrar]))
    return TestClient(app)


class TestHttpChannelBroadcast:
    def test_channel_3_frame_accepted(self, client):
        with client.websocket_connect("/tunnel/test-http") as ws:
            msg = json.dumps({"type": "http_req", "id": "r1", "method": "GET", "url": "/test"}).encode()
            ws.send_bytes(encode_frame(CHANNEL_HTTP, msg))

    def test_invalid_json_on_channel_3(self, client):
        with client.websocket_connect("/tunnel/test-http2") as ws:
            ws.send_bytes(encode_frame(CHANNEL_HTTP, b"not json"))

    def test_terminal_and_http_coexist(self, client):
        with client.websocket_connect("/tunnel/test-http3") as ws:
            ws.send_bytes(encode_frame(CHANNEL_DATA, b"terminal log"))
            ws.send_bytes(
                encode_frame(
                    CHANNEL_HTTP, json.dumps({"type": "http_req", "id": "r1", "method": "GET", "url": "/"}).encode()
                )
            )

    def test_http_response_frame(self, client):
        with client.websocket_connect("/tunnel/test-http4") as ws:
            ws.send_bytes(
                encode_frame(
                    CHANNEL_HTTP,
                    json.dumps(
                        {"type": "http_res", "id": "r1", "status": 200, "status_text": "OK", "duration_ms": 42}
                    ).encode(),
                )
            )
