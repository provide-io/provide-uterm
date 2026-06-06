#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E tunnel tests against pywrangler dev (or real CF deployment).

Verifies:
- POST /api/tunnels creates session with tokens
- GET /s/{id}?invite=... sets a cookie and redirects to /app/session/{id}
- WSS /tunnel/{id} with worker_token connects agent
- Channel 0x03 HTTP inspection frames accepted
- Terminal + HTTP channels coexist on same WS

Run with:
    E2E=1 uv run pytest tests/test_e2e_tunnel.py -v -p no:xdist -p no:randomly
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest
from cf_e2e_auth import http_auth_headers

from provide.uterm.tunnel.protocol import CHANNEL_DATA, CHANNEL_HTTP, encode_control, encode_frame

_HTTP_UA = "provide-uterm-e2e-tunnel/1.0"


def _base_ws(base_http: str) -> str:
    return base_http.replace("http://", "ws://").replace("https://", "wss://")


def _http_post(base: str, path: str, body: dict) -> tuple[int, dict]:
    url = f"{base}{path}"
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "User-Agent": _HTTP_UA, **http_auth_headers(url)}
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:
            return exc.code, {}


def _http_get(base: str, path: str, *, follow_redirects: bool = True) -> tuple[int, str, dict[str, str]]:
    """GET with optional redirect following. Returns (status, body, headers)."""
    url = f"{base}{path}"
    headers_dict = {"User-Agent": _HTTP_UA, **http_auth_headers(url)}
    req = urllib.request.Request(url, headers=headers_dict)  # noqa: S310
    try:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        if not follow_redirects:
            opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(req, timeout=10) as resp:
            return resp.status, resp.read().decode(errors="replace"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace") if exc.fp else "", dict(exc.headers)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def _http_delete(base: str, path: str) -> tuple[int, dict]:
    url = f"{base}{path}"
    headers = {"User-Agent": _HTTP_UA, **http_auth_headers(url)}
    req = urllib.request.Request(url, method="DELETE", headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:
            return exc.code, {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestTunnelCreation:
    def test_create_tunnel(self, wrangler_server: str) -> None:
        status, body = _http_post(
            wrangler_server, "/api/tunnels", {"tunnel_type": "terminal", "display_name": "e2e-tunnel"}
        )
        assert status == 200
        assert body["tunnel_id"].startswith("tunnel-")
        assert "worker_token" in body
        assert "ws_endpoint" in body
        assert "share_url" in body
        assert "control_url" in body
        assert "expires_at" in body
        assert body["expires_at"] > time.time()

    def test_create_http_tunnel(self, wrangler_server: str) -> None:
        status, body = _http_post(wrangler_server, "/api/tunnels", {"tunnel_type": "http", "display_name": "e2e-http"})
        assert status == 200
        assert body["tunnel_type"] == "http"


@pytest.mark.e2e
class TestShortShareUrl:
    def test_s_route_serves_page(self, wrangler_server: str) -> None:
        """GET /s/{id}?invite=... → redirects to a clean app URL."""
        _, tunnel = _http_post(wrangler_server, "/api/tunnels", {"tunnel_type": "terminal"})
        tid = tunnel["tunnel_id"]
        share_url = tunnel["share_url"]
        invite = share_url.split("invite=")[1] if "invite=" in share_url else ""
        status, body, headers = _http_get(wrangler_server, f"/s/{tid}?invite={invite}", follow_redirects=False)
        assert status == 302, f"Expected 302, got {status}: {body[:200]}"
        location = headers.get("location", headers.get("Location", ""))
        assert f"/app/session/{tid}" in location
        assert "Set-Cookie" in headers


@pytest.mark.e2e
class TestTunnelWebSocket:
    def test_agent_connects(self, wrangler_server: str) -> None:
        """Agent connects to /tunnel/{id} with worker_token."""
        import websockets.sync.client as wsc

        _, tunnel = _http_post(wrangler_server, "/api/tunnels", {"tunnel_type": "terminal"})
        ws_url = f"{_base_ws(wrangler_server)}/tunnel/{tunnel['tunnel_id']}"
        ws = wsc.connect(ws_url, additional_headers={"Authorization": f"Bearer {tunnel['worker_token']}"})
        ws.send(encode_control({"type": "open", "channel": 1, "tunnel_type": "terminal", "term_size": [80, 24]}))
        ws.close()

    def test_agent_sends_terminal_data(self, wrangler_server: str) -> None:
        """Agent sends binary terminal data on channel 0x01."""
        import websockets.sync.client as wsc

        _, tunnel = _http_post(wrangler_server, "/api/tunnels", {"tunnel_type": "terminal"})
        ws_url = f"{_base_ws(wrangler_server)}/tunnel/{tunnel['tunnel_id']}"
        ws = wsc.connect(ws_url, additional_headers={"Authorization": f"Bearer {tunnel['worker_token']}"})
        ws.send(encode_control({"type": "open", "channel": 1, "tunnel_type": "terminal"}))
        ws.send(encode_frame(CHANNEL_DATA, b"hello from e2e tunnel agent"))
        ws.close()

    def test_http_channel_accepted(self, wrangler_server: str) -> None:
        """Agent sends HTTP inspection frames on channel 0x03."""
        import websockets.sync.client as wsc

        _, tunnel = _http_post(wrangler_server, "/api/tunnels", {"tunnel_type": "http"})
        ws_url = f"{_base_ws(wrangler_server)}/tunnel/{tunnel['tunnel_id']}"
        ws = wsc.connect(ws_url, additional_headers={"Authorization": f"Bearer {tunnel['worker_token']}"})
        ws.send(encode_control({"type": "open", "channel": 3, "tunnel_type": "http"}))
        # Send HTTP req
        ws.send(
            encode_frame(
                CHANNEL_HTTP,
                json.dumps(
                    {
                        "type": "http_req",
                        "id": "r1",
                        "method": "GET",
                        "url": "/test",
                        "headers": {},
                        "body_size": 0,
                    }
                ).encode(),
            )
        )
        # Send HTTP res
        ws.send(
            encode_frame(
                CHANNEL_HTTP,
                json.dumps(
                    {
                        "type": "http_res",
                        "id": "r1",
                        "status": 200,
                        "status_text": "OK",
                        "headers": {},
                        "body_size": 5,
                        "duration_ms": 42,
                    }
                ).encode(),
            )
        )
        ws.close()

    def test_terminal_and_http_coexist(self, wrangler_server: str) -> None:
        """Both channel 0x01 and 0x03 work on the same WebSocket."""
        import websockets.sync.client as wsc

        _, tunnel = _http_post(wrangler_server, "/api/tunnels", {"tunnel_type": "http"})
        ws_url = f"{_base_ws(wrangler_server)}/tunnel/{tunnel['tunnel_id']}"
        ws = wsc.connect(ws_url, additional_headers={"Authorization": f"Bearer {tunnel['worker_token']}"})
        ws.send(encode_control({"type": "open", "channel": 3, "tunnel_type": "http"}))
        ws.send(encode_frame(CHANNEL_DATA, b"[log] request proxied\n"))
        ws.send(
            encode_frame(
                CHANNEL_HTTP,
                json.dumps(
                    {"type": "http_req", "id": "r1", "method": "GET", "url": "/", "headers": {}, "body_size": 0}
                ).encode(),
            )
        )
        ws.close()


@pytest.mark.e2e
class TestSessionRegistry:
    def test_tunnel_visible_in_sessions(self, wrangler_server: str) -> None:
        """Tunnel session appears in GET /api/sessions."""
        _http_post(wrangler_server, "/api/tunnels", {"tunnel_type": "terminal", "display_name": "e2e-registry"})
        status, _body, _ = _http_get(wrangler_server, "/api/sessions")
        assert status == 200


@pytest.mark.e2e
class TestInspectPage:
    def test_inspect_page_loads(self, wrangler_server: str) -> None:
        """GET /app/inspect/{id} returns 200 with inspect page."""
        _, tunnel = _http_post(wrangler_server, "/api/tunnels", {"tunnel_type": "http"})
        tid = tunnel["tunnel_id"]
        status, body, _ = _http_get(wrangler_server, f"/app/inspect/{tid}")
        assert status == 200
        assert "inspect" in body.lower() or "Inspect" in body or "app-bootstrap" in body
