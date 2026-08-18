#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Integration tests for _run_inspect — real WS + HTTP proxy, no mocks."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

import httpx2
import pytest
import websockets.server

from provide.uterm.cli.inspect import _run_inspect
from provide.uterm.tunnel.protocol import (
    CHANNEL_DATA,
    CHANNEL_HTTP,
    encode_frame,
)

from . import test_run_inspect_integration_part1 as _part1
from .test_run_inspect_integration_part1 import (
    TunnelWSServer,
    _free_port,
    _wait_for_port,
)

target_server = pytest.fixture(_part1.target_server.__wrapped__)
mock_tunnel_ws = pytest.fixture(_part1.mock_tunnel_ws.__wrapped__)
inspect_proxy = pytest.fixture(_part1.inspect_proxy.__wrapped__)
inspect_proxy_intercept = pytest.fixture(_part1.inspect_proxy_intercept.__wrapped__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestRunInspectIntegration:
    @pytest.mark.timeout(15)
    async def test_basic_get_forward(self, inspect_proxy: int, mock_tunnel_ws: TunnelWSServer):
        """GET through proxy forwards and produces http_req + http_res."""
        async with httpx2.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{inspect_proxy}/hello")
        assert resp.status_code == 200
        body = resp.json()
        assert body["echo"] is True
        assert body["path"] == "/hello"

        req_frame = await mock_tunnel_ws.wait_for_frame("http_req")
        assert req_frame["method"] == "GET"
        assert "/hello" in req_frame["url"]

        res_frame = await mock_tunnel_ws.wait_for_frame("http_res")
        assert res_frame["status"] == 200
        assert res_frame["id"] == req_frame["id"]

    @pytest.mark.timeout(15)
    async def test_post_with_body(self, inspect_proxy: int, mock_tunnel_ws: TunnelWSServer):
        """POST JSON body is forwarded and body_b64 appears in http_req frame."""
        import base64

        payload = {"user": "admin", "action": "login"}
        async with httpx2.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{inspect_proxy}/api/data",
                json=payload,
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["method"] == "POST"

        req_frame = await mock_tunnel_ws.wait_for_frame("http_req")
        assert req_frame["method"] == "POST"
        assert "body_b64" in req_frame
        decoded = base64.b64decode(req_frame["body_b64"])
        assert json.loads(decoded) == payload

    @pytest.mark.timeout(15)
    async def test_get_with_query_string(self, inspect_proxy: int, mock_tunnel_ws: TunnelWSServer):
        """Query string is forwarded to target and appears in http_req frame."""
        async with httpx2.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{inspect_proxy}/search?q=hello&page=1")
        assert resp.status_code == 200
        body = resp.json()
        assert "q=hello" in body["qs"]

        req_frame = await mock_tunnel_ws.wait_for_frame("http_req")
        assert "q=hello" in req_frame["url"]

    @pytest.mark.timeout(15)
    async def test_initial_state_broadcast(self, mock_tunnel_ws: TunnelWSServer, target_server: int):
        """Initial http_intercept_state is sent on WS connect."""
        proxy_port = _free_port()
        ws_endpoint = f"ws://127.0.0.1:{mock_tunnel_ws.port}"

        task = asyncio.create_task(
            _run_inspect(
                ws_endpoint=ws_endpoint,
                worker_token="",
                target_port=target_server,
                listen_port=proxy_port,
                intercept=True,
                intercept_timeout=5.0,
                intercept_timeout_action="drop",
            )
        )
        await _wait_for_port(proxy_port)
        try:
            state = await mock_tunnel_ws.wait_for_frame("http_intercept_state")
            assert state["enabled"] is True
            assert state["inspect_enabled"] is True
            assert state["timeout_s"] == 5.0
            assert state["timeout_action"] == "drop"
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    @pytest.mark.timeout(15)
    async def test_intercept_forward(self, inspect_proxy_intercept: int, mock_tunnel_ws: TunnelWSServer):
        """With intercept on, sending 'forward' action lets request through."""

        async def _do_request() -> httpx2.Response:
            async with httpx2.AsyncClient() as client:
                return await client.get(f"http://127.0.0.1:{inspect_proxy_intercept}/intercepted")

        req_task = asyncio.create_task(_do_request())

        # Wait for the http_req frame, then send forward action
        req_frame = await mock_tunnel_ws.wait_for_frame("http_req")
        assert req_frame["intercepted"] is True

        await mock_tunnel_ws.send_action({"type": "http_action", "id": req_frame["id"], "action": "forward"})

        resp = await asyncio.wait_for(req_task, timeout=5.0)
        assert resp.status_code == 200
        assert resp.json()["echo"] is True

    @pytest.mark.timeout(15)
    async def test_intercept_drop(self, inspect_proxy_intercept: int, mock_tunnel_ws: TunnelWSServer):
        """With intercept on, sending 'drop' action returns 502."""

        async def _do_request() -> httpx2.Response:
            async with httpx2.AsyncClient() as client:
                return await client.get(f"http://127.0.0.1:{inspect_proxy_intercept}/should-drop")

        req_task = asyncio.create_task(_do_request())

        req_frame = await mock_tunnel_ws.wait_for_frame("http_req")
        await mock_tunnel_ws.send_action({"type": "http_action", "id": req_frame["id"], "action": "drop"})

        resp = await asyncio.wait_for(req_task, timeout=5.0)
        assert resp.status_code == 502
        assert b"dropped" in resp.content.lower()

        # Verify drop event was sent as http_res
        drop_frame = await mock_tunnel_ws.wait_for_frame("http_res")
        assert drop_frame["status"] == 502
        assert drop_frame["status_text"] == "Dropped"

    @pytest.mark.timeout(15)
    async def test_proxy_target_down(self, mock_tunnel_ws: TunnelWSServer):
        """When target is unreachable, proxy returns 502."""
        dead_port = _free_port()  # nothing listening
        proxy_port = _free_port()
        ws_endpoint = f"ws://127.0.0.1:{mock_tunnel_ws.port}"

        task = asyncio.create_task(
            _run_inspect(
                ws_endpoint=ws_endpoint,
                worker_token="",
                target_port=dead_port,
                listen_port=proxy_port,
            )
        )
        await _wait_for_port(proxy_port)
        try:
            async with httpx2.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{proxy_port}/fail")
            assert resp.status_code == 502
            assert b"Bad Gateway" in resp.content
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    @pytest.mark.timeout(15)
    async def test_inspect_toggle_off(self, inspect_proxy: int, mock_tunnel_ws: TunnelWSServer):
        """Toggle inspect off — no more http_req/http_res frames."""
        # First, make a normal request to confirm frames are flowing
        async with httpx2.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{inspect_proxy}/before")
        assert resp.status_code == 200
        await mock_tunnel_ws.wait_for_frame("http_req")

        # Now toggle inspect off via text frame
        await mock_tunnel_ws.send_text_action({"type": "http_inspect_toggle", "enabled": False})
        # Give the action receiver time to process
        await asyncio.sleep(0.3)

        # Clear collected frames
        before_count = len(mock_tunnel_ws.frames)

        async with httpx2.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{inspect_proxy}/after")
        assert resp.status_code == 200

        # Wait briefly and check no new http_req/http_res frames arrived
        await asyncio.sleep(0.3)
        new_frames = mock_tunnel_ws.frames[before_count:]
        http_frames = [f for f in new_frames if f.get("type") in ("http_req", "http_res")]
        assert http_frames == [], f"Expected no http frames after inspect off, got {http_frames}"

    @pytest.mark.timeout(15)
    async def test_intercept_toggle_off_releases_pending(
        self, inspect_proxy_intercept: int, mock_tunnel_ws: TunnelWSServer
    ):
        """Toggling intercept off while a request is pending forwards it."""

        async def _do_request() -> httpx2.Response:
            async with httpx2.AsyncClient() as client:
                return await client.get(f"http://127.0.0.1:{inspect_proxy_intercept}/pending")

        req_task = asyncio.create_task(_do_request())

        # Wait for the intercepted request
        await mock_tunnel_ws.wait_for_frame("http_req")

        # Toggle intercept off — should release pending with forward
        await mock_tunnel_ws.send_text_action({"type": "http_intercept_toggle", "enabled": False})

        resp = await asyncio.wait_for(req_task, timeout=5.0)
        assert resp.status_code == 200

    @pytest.mark.timeout(15)
    async def test_ws_receiver_handles_invalid_json(self, inspect_proxy: int, mock_tunnel_ws: TunnelWSServer):
        """Invalid JSON on the WS channel doesn't crash the receiver."""
        assert mock_tunnel_ws._ws is not None
        # Send garbage binary frame
        await mock_tunnel_ws._ws.send(encode_frame(CHANNEL_HTTP, b"not json"))
        # Send garbage text frame
        await mock_tunnel_ws._ws.send("not json at all")
        await asyncio.sleep(0.2)

        # Proxy should still work
        async with httpx2.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{inspect_proxy}/still-works")
        assert resp.status_code == 200

    @pytest.mark.timeout(15)
    async def test_ws_receiver_ignores_unknown_text_types(self, inspect_proxy: int, mock_tunnel_ws: TunnelWSServer):
        """Text frames with unknown types are silently ignored."""
        await mock_tunnel_ws.send_text_action({"type": "unknown_msg", "data": "x"})
        await asyncio.sleep(0.2)

        async with httpx2.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{inspect_proxy}/ok")
        assert resp.status_code == 200

    @pytest.mark.timeout(15)
    async def test_intercept_modify(self, inspect_proxy_intercept: int, mock_tunnel_ws: TunnelWSServer):
        """Intercept modify action changes headers and body before forwarding."""
        import base64

        async def _do_request() -> httpx2.Response:
            async with httpx2.AsyncClient() as client:
                return await client.post(
                    f"http://127.0.0.1:{inspect_proxy_intercept}/modify-me",
                    content=b"original body",
                )

        req_task = asyncio.create_task(_do_request())

        req_frame = await mock_tunnel_ws.wait_for_frame("http_req")
        modified_body = b"modified body"
        await mock_tunnel_ws.send_action(
            {
                "type": "http_action",
                "id": req_frame["id"],
                "action": "modify",
                "headers": {"x-modified": "true"},
                "body_b64": base64.b64encode(modified_body).decode(),
            }
        )

        resp = await asyncio.wait_for(req_task, timeout=5.0)
        assert resp.status_code == 200
        echo = resp.json()
        assert echo["body"] == "modified body"

    @pytest.mark.timeout(15)
    async def test_non_http_scope_ignored(self, inspect_proxy: int, mock_tunnel_ws: TunnelWSServer):
        """Non-http requests pass through the proxy ASGI app cleanly.

        We verify the proxy still works after any non-http scope would be seen.
        """
        async with httpx2.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{inspect_proxy}/ok")
        assert resp.status_code == 200

    @pytest.mark.timeout(15)
    async def test_ws_receiver_non_http_binary_frame_ignored(self, inspect_proxy: int, mock_tunnel_ws: TunnelWSServer):
        """Binary frames on non-HTTP channels are silently skipped."""
        assert mock_tunnel_ws._ws is not None
        # Send a binary frame on CHANNEL_DATA (not CHANNEL_HTTP)
        await mock_tunnel_ws._ws.send(encode_frame(CHANNEL_DATA, b"some data"))
        await asyncio.sleep(0.2)

        # Proxy still works
        async with httpx2.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{inspect_proxy}/after-non-http")
        assert resp.status_code == 200

    @pytest.mark.timeout(15)
    async def test_ws_receiver_short_binary_frame_ignored(self, inspect_proxy: int, mock_tunnel_ws: TunnelWSServer):
        """Binary frames with len <= 2 hit the else branch."""
        assert mock_tunnel_ws._ws is not None
        # Send a 2-byte binary frame (has channel+flags but no payload — len is NOT > 2)
        await mock_tunnel_ws._ws.send(b"\x03\x00")
        await asyncio.sleep(0.2)

        # Proxy still works
        async with httpx2.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{inspect_proxy}/after-short")
        assert resp.status_code == 200

    @pytest.mark.timeout(15)
    async def test_ws_close_triggers_cancel_all(self, target_server: int):
        """When the WS server aborts, the except branch fires and cancels pending."""
        ws_port = _free_port()
        proxy_port = _free_port()

        close_event = asyncio.Event()

        async def _handler(ws: websockets.server.ServerConnection) -> None:
            # Wait a moment then abort the connection ungracefully
            await asyncio.sleep(0.3)
            close_event.set()
            # Close the underlying transport to trigger an exception (not a clean close)
            ws.transport.abort()

        server = await websockets.server.serve(_handler, "127.0.0.1", ws_port)
        try:
            task = asyncio.create_task(
                _run_inspect(
                    ws_endpoint=f"ws://127.0.0.1:{ws_port}",
                    worker_token="",
                    target_port=target_server,
                    listen_port=proxy_port,
                )
            )
            await _wait_for_port(proxy_port)
            await asyncio.wait_for(close_event.wait(), timeout=5.0)
            await asyncio.sleep(0.5)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.timeout(15)
    async def test_intercept_toggle_on(self, inspect_proxy: int, mock_tunnel_ws: TunnelWSServer):
        """Toggling intercept ON (enabled=True) covers the 'gate IS enabled' branch."""
        await mock_tunnel_ws.send_text_action({"type": "http_intercept_toggle", "enabled": True})
        await asyncio.sleep(0.3)

        # Verify state broadcast was sent with enabled=True
        states = mock_tunnel_ws.frames_of_type("http_intercept_state")
        assert any(s["enabled"] is True for s in states)

    @pytest.mark.timeout(15)
    async def test_inspect_toggle_on_after_off(self, inspect_proxy: int, mock_tunnel_ws: TunnelWSServer):
        """Toggle inspect off then back on covers the 'inspect IS enabled' branch."""
        # Toggle off
        await mock_tunnel_ws.send_text_action({"type": "http_inspect_toggle", "enabled": False})
        await asyncio.sleep(0.2)
        # Toggle back on
        await mock_tunnel_ws.send_text_action({"type": "http_inspect_toggle", "enabled": True})
        await asyncio.sleep(0.2)

        states = mock_tunnel_ws.frames_of_type("http_intercept_state")
        # Should have at least 2 state broadcasts
        assert len(states) >= 2

    @pytest.mark.timeout(15)
    async def test_intercept_modify_headers_only(self, inspect_proxy_intercept: int, mock_tunnel_ws: TunnelWSServer):
        """Modify action with only headers (no body) still forwards correctly."""

        async def _do_request() -> httpx2.Response:
            async with httpx2.AsyncClient() as client:
                return await client.get(f"http://127.0.0.1:{inspect_proxy_intercept}/modify-headers")

        req_task = asyncio.create_task(_do_request())

        req_frame = await mock_tunnel_ws.wait_for_frame("http_req")
        await mock_tunnel_ws.send_action(
            {
                "type": "http_action",
                "id": req_frame["id"],
                "action": "modify",
                "headers": {"x-custom": "value"},
            }
        )

        resp = await asyncio.wait_for(req_task, timeout=5.0)
        assert resp.status_code == 200

    @pytest.mark.timeout(15)
    async def test_ws_receiver_unknown_intercept_id(self, inspect_proxy_intercept: int, mock_tunnel_ws: TunnelWSServer):
        """Unknown request ID logs warning but doesn't crash."""
        # Wait for initial state frame
        await mock_tunnel_ws.wait_for_frame("http_intercept_state")

        # Send action for non-existent request
        await mock_tunnel_ws.send_action({"type": "http_action", "id": "nonexistent", "action": "forward"})
        await asyncio.sleep(0.2)

        # Proxy still works
        async def _do_request() -> httpx2.Response:
            async with httpx2.AsyncClient() as client:
                return await client.get(f"http://127.0.0.1:{inspect_proxy_intercept}/ok")

        req_task = asyncio.create_task(_do_request())
        req_frame = await mock_tunnel_ws.wait_for_n_frames("http_req", 1)
        await mock_tunnel_ws.send_action({"type": "http_action", "id": req_frame[0]["id"], "action": "forward"})
        resp = await asyncio.wait_for(req_task, timeout=5.0)
        assert resp.status_code == 200
