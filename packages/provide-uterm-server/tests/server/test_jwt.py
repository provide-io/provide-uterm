#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Live integration tests for JWT-authenticated hosted server flows."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

import httpx
import jwt
import pytest
import uvicorn
import websockets
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from provide.uterm.control_channel import ControlChannelDecoder, ControlChunk, DataChunk, encode_control, encode_data
from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.tunnel.protocol import CHANNEL_DATA, encode_frame

if TYPE_CHECKING:
    from collections.abc import Generator

_TEST_SIGNING_KEY = "uterm-jwt-e2e-secret-32-byte-minimum-key"


def _ws_url(base_url: str, path: str) -> str:
    return base_url.replace("http://", "ws://") + path


def _mint_token(subject: str, roles: list[str], *, lifetime_s: int = 600) -> str:
    now = int(time.time())
    return str(
        jwt.encode(
            {
                "sub": subject,
                "roles": roles,
                "iss": "provide-uterm",
                "aud": "provide-uterm-server",
                "iat": now,
                "nbf": now,
                "exp": now + lifetime_s,
            },
            key=_TEST_SIGNING_KEY,
            algorithm="HS256",
        )
    )


def _auth_headers(subject: str, roles: list[str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {_mint_token(subject, roles)}"}


_WS_DECODERS: WeakKeyDictionary[Any, ControlChannelDecoder] = WeakKeyDictionary()
_WS_PENDING: WeakKeyDictionary[Any, list[dict[str, Any]]] = WeakKeyDictionary()


async def _drain_until(ws: Any, type_: str, timeout: float = 5.0) -> dict[str, Any] | None:
    decoder = _WS_DECODERS.setdefault(ws, ControlChannelDecoder())
    pending = _WS_PENDING.setdefault(ws, [])
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            if pending:
                msg = pending.pop(0)
                if msg.get("type") == type_:
                    return msg
                continue
            raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            events = decoder.feed(raw)
            for event in events:
                if isinstance(event, ControlChunk):
                    pending.append(event.control)
                elif isinstance(event, DataChunk):
                    pending.append({"type": "term", "data": event.data})
            if not pending:
                continue
            msg = pending.pop(0)
            if msg.get("type") == type_:
                return msg
        except TimeoutError:
            continue
    return None


async def _send_frame(ws: Any, payload: dict[str, Any]) -> None:
    frame_type = payload.get("type")
    if frame_type in {"input", "term"}:
        await ws.send(encode_data(str(payload.get("data", ""))))
        return
    await ws.send(encode_control(payload))


async def _wait_for_hijack_state(ws: Any, *, expected: bool, timeout: float = 5.0) -> dict[str, Any] | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        state = await _drain_until(ws, "hijack_state", timeout=0.7)
        if state is None:
            continue
        if bool(state.get("hijacked")) is expected:
            return state
    return None


@pytest.fixture()
def live_reference_server_jwt() -> Generator[str, None, None]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    base_url = f"http://127.0.0.1:{port}"
    config = default_server_config()
    # Start the shell session in hijack mode so worker_hello echoes "hijack"
    # rather than "open", avoiding a race where the worker re-sends open mode
    # after a test-initiated mode switch.
    for session in config.sessions:
        if session.session_id == "provide-shell":
            session.input_mode = "hijack"
    config.auth.mode = "jwt"
    config.auth.jwt_public_key_pem = _TEST_SIGNING_KEY
    config.auth.jwt_algorithms = ["HS256"]
    config.auth.worker_bearer_token = _mint_token("runtime-worker", ["admin"])
    config.server.host = "127.0.0.1"
    config.server.port = port
    config.server.public_base_url = base_url

    app = create_server_app(config)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("reference server did not start")
        time.sleep(0.05)

    yield base_url

    server.should_exit = True
    thread.join(timeout=5)


class TestReferenceServerJwtE2E:
    async def _wait_for_connected(self, base_url: str, session_id: str, headers: dict[str, str]) -> None:
        async with httpx.AsyncClient(base_url=base_url) as http:
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                resp = await http.get(f"/api/sessions/{session_id}", headers=headers)
                if resp.status_code == 200 and resp.json()["connected"] is True:
                    return
                await asyncio.sleep(0.1)
        raise AssertionError(f"session did not become connected: {session_id}")

    async def test_jwt_owner_and_admin_api_authorization(self, live_reference_server_jwt: str) -> None:
        operator_headers = _auth_headers("op-1", ["operator"])
        admin_headers = _auth_headers("admin-1", ["admin"])

        async with httpx.AsyncClient(base_url=live_reference_server_jwt) as http:
            created = await http.post(
                "/api/sessions",
                headers=operator_headers,
                json={
                    "session_id": "jwt-owned",
                    "display_name": "JWT Owned",
                    "connector_type": "shell",
                    "auto_start": True,
                },
            )
            assert created.status_code == 200

            await self._wait_for_connected(live_reference_server_jwt, "jwt-owned", operator_headers)

            forbidden_delete = await http.delete("/api/sessions/jwt-owned", headers=operator_headers)
            assert forbidden_delete.status_code == 403

            allowed_mode = await http.post(
                "/api/sessions/jwt-owned/mode", headers=operator_headers, json={"input_mode": "hijack"}
            )
            assert allowed_mode.status_code == 200

            admin_delete = await http.delete("/api/sessions/jwt-owned", headers=admin_headers)
            assert admin_delete.status_code == 200

    async def test_jwt_browser_ws_enforces_hijack_privileges(self, live_reference_server_jwt: str) -> None:
        admin_headers = _auth_headers("admin-1", ["admin"])
        viewer_headers = _auth_headers("viewer-1", ["viewer"])

        await self._wait_for_connected(live_reference_server_jwt, "provide-shell", admin_headers)

        async with websockets.connect(
            _ws_url(live_reference_server_jwt, "/ws/browser/provide-shell/term"), additional_headers=viewer_headers
        ) as viewer_ws:
            viewer_hello = await _drain_until(viewer_ws, "hello")
            assert viewer_hello is not None
            assert viewer_hello["role"] == "viewer"
            assert viewer_hello["can_hijack"] is False
            await _send_frame(viewer_ws, {"type": "hijack_request"})
            viewer_error = await _drain_until(viewer_ws, "error")
            assert viewer_error is not None
            assert "admin" in str(viewer_error.get("message", "")).lower()

        async with websockets.connect(
            _ws_url(live_reference_server_jwt, "/ws/browser/provide-shell/term"), additional_headers=admin_headers
        ) as admin_ws:
            admin_hello = await _drain_until(admin_ws, "hello")
            assert admin_hello is not None
            assert admin_hello["role"] == "admin"
            assert admin_hello["can_hijack"] is True
            await _send_frame(admin_ws, {"type": "hijack_request"})
            hijack_state = await _wait_for_hijack_state(admin_ws, expected=True)
            assert hijack_state is not None
            assert hijack_state["hijacked"] is True

    async def test_tunnel_route_accepts_tunnel_worker_token_in_jwt_mode(self) -> None:
        """A valid tunnel worker token should connect to /tunnel/{id} even in JWT mode."""
        config = default_server_config()
        config.auth.mode = "jwt"
        config.auth.jwt_public_key_pem = _TEST_SIGNING_KEY
        config.auth.jwt_algorithms = ["HS256"]
        config.auth.worker_bearer_token = _mint_token("runtime-worker", ["admin"])
        config.server.host = "127.0.0.1"
        config.server.port = 0

        app = create_server_app(config)
        admin_headers = _auth_headers("jwt-admin", ["admin"])
        with TestClient(app) as client:
            created = client.post("/api/tunnels", headers=admin_headers, json={"tunnel_type": "terminal"})
            assert created.status_code == 200
            tunnel = created.json()
            token = tunnel["worker_token"]
            tunnel_id = tunnel["tunnel_id"]
            ws_endpoint = f"/tunnel/{tunnel_id}"

            with client.websocket_connect(ws_endpoint, headers={"Authorization": f"Bearer {token}"}) as ws:
                ws.send_bytes(encode_frame(CHANNEL_DATA, b"hello from jwt worker"))

    async def test_tunnel_route_accepts_global_worker_token_in_jwt_mode(self) -> None:
        """Global runtime worker token should still authenticate /tunnel/{id} in JWT mode."""
        config = default_server_config()
        config.auth.mode = "jwt"
        config.auth.jwt_public_key_pem = _TEST_SIGNING_KEY
        config.auth.jwt_algorithms = ["HS256"]
        global_worker_token = _mint_token("runtime-worker", ["admin"])
        config.auth.worker_bearer_token = global_worker_token
        config.server.host = "127.0.0.1"
        config.server.port = 0

        app = create_server_app(config)
        admin_headers = _auth_headers("jwt-admin", ["admin"])
        with TestClient(app) as client:
            created = client.post("/api/tunnels", headers=admin_headers, json={"tunnel_type": "terminal"})
            assert created.status_code == 200
            tunnel_id = created.json()["tunnel_id"]
            ws_endpoint = f"/tunnel/{tunnel_id}"

            with client.websocket_connect(
                ws_endpoint,
                headers={"Authorization": f"Bearer {global_worker_token}"},
            ) as ws:
                ws.send_bytes(encode_frame(CHANNEL_DATA, b"hello with global worker token"))

    async def test_tunnel_route_rejects_bad_token_in_jwt_mode(self) -> None:
        """A tunnel endpoint should reject incorrect worker tokens in JWT mode."""
        config = default_server_config()
        config.auth.mode = "jwt"
        config.auth.jwt_public_key_pem = _TEST_SIGNING_KEY
        config.auth.jwt_algorithms = ["HS256"]
        config.auth.worker_bearer_token = _mint_token("runtime-worker", ["admin"])
        config.server.host = "127.0.0.1"
        config.server.port = 0

        app = create_server_app(config)
        admin_headers = _auth_headers("jwt-admin", ["admin"])
        with TestClient(app) as client:
            created = client.post("/api/tunnels", headers=admin_headers, json={"tunnel_type": "terminal"})
            assert created.status_code == 200
            tunnel_id = created.json()["tunnel_id"]
            ws_endpoint = f"/tunnel/{tunnel_id}"

            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(
                    ws_endpoint,
                    headers={"Authorization": "Bearer definitely-wrong-token"},
                ):
                    pass

    async def test_tunnel_route_rejects_expired_tunnel_worker_token_in_jwt_mode(self) -> None:
        """Expired per-session tunnel worker tokens should not authenticate /tunnel/{id}."""
        config = default_server_config()
        config.auth.mode = "jwt"
        config.auth.jwt_public_key_pem = _TEST_SIGNING_KEY
        config.auth.jwt_algorithms = ["HS256"]
        config.auth.worker_bearer_token = _mint_token("runtime-worker", ["admin"])
        config.server.host = "127.0.0.1"
        config.server.port = 0

        app = create_server_app(config)
        admin_headers = _auth_headers("jwt-admin", ["admin"])
        with TestClient(app) as client:
            created = client.post("/api/tunnels", headers=admin_headers, json={"tunnel_type": "terminal"})
            assert created.status_code == 200
            tunnel = created.json()
            token = tunnel["worker_token"]
            tunnel_id = tunnel["tunnel_id"]
            ws_endpoint = f"/tunnel/{tunnel_id}"

            client.app.state.uterm_tunnel_tokens[tunnel_id]["expires_at"] = time.time() - 1

            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(
                    ws_endpoint,
                    headers={"Authorization": f"Bearer {token}"},
                ):
                    pass

    async def test_tunnel_route_rejects_worker_token_with_ip_binding_mismatch(self) -> None:
        """IP-bound tunnel workers should be rejected if the source IP changes."""
        config = default_server_config()
        config.auth.mode = "jwt"
        config.auth.jwt_public_key_pem = _TEST_SIGNING_KEY
        config.auth.jwt_algorithms = ["HS256"]
        config.auth.worker_bearer_token = _mint_token("runtime-worker", ["admin"])
        config.tunnel.ip_binding = True
        config.server.host = "127.0.0.1"
        config.server.port = 0

        app = create_server_app(config)
        admin_headers = _auth_headers("jwt-admin", ["admin"])
        with TestClient(app) as client:
            created = client.post("/api/tunnels", headers=admin_headers, json={"tunnel_type": "terminal"})
            assert created.status_code == 200
            tunnel = created.json()
            token = tunnel["worker_token"]
            tunnel_id = tunnel["tunnel_id"]
            ws_endpoint = f"/tunnel/{tunnel_id}"

            client.app.state.uterm_tunnel_tokens[tunnel_id]["issued_ip"] = "198.51.100.1"

            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(
                    ws_endpoint,
                    headers={"Authorization": f"Bearer {token}"},
                ):
                    pass

    async def test_tunnel_route_rejects_when_tunnel_token_store_is_corrupt(self) -> None:
        """A non-dict tunnel token store should be treated as missing worker token data."""
        config = default_server_config()
        config.auth.mode = "jwt"
        config.auth.jwt_public_key_pem = _TEST_SIGNING_KEY
        config.auth.jwt_algorithms = ["HS256"]
        config.auth.worker_bearer_token = _mint_token("runtime-worker", ["admin"])
        config.server.host = "127.0.0.1"
        config.server.port = 0

        app = create_server_app(config)
        admin_headers = _auth_headers("jwt-admin", ["admin"])
        with TestClient(app) as client:
            created = client.post("/api/tunnels", headers=admin_headers, json={"tunnel_type": "terminal"})
            assert created.status_code == 200
            tunnel_id = created.json()["tunnel_id"]
            ws_endpoint = f"/tunnel/{tunnel_id}"

            client.app.state.uterm_tunnel_tokens = {"__poison": object()}

            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(ws_endpoint, headers={"Authorization": "Bearer no-match"}):
                    pass
