#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Live integration tests for JWT-authenticated hosted server flows."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from typing import TYPE_CHECKING, Any

import httpx
import jwt
import pytest
import uvicorn
import websockets

from provide.terminal.server import create_server_app, default_server_config

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
                "iss": "provide-terminal",
                "aud": "provide-terminal-server",
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


async def _drain_until(ws: Any, type_: str, timeout: float = 5.0) -> dict[str, Any] | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            msg = json.loads(raw)
            if msg.get("type") == type_:
                return msg
        except TimeoutError:
            continue
    return None


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
        async with httpx.AsyncClient(base_url=live_reference_server_jwt) as http:
            mode = await http.post(
                "/api/sessions/provide-shell/mode",
                headers=admin_headers,
                json={"input_mode": "hijack"},
            )
            assert mode.status_code == 200

        async with websockets.connect(
            _ws_url(live_reference_server_jwt, "/ws/browser/provide-shell/term"), additional_headers=viewer_headers
        ) as viewer_ws:
            viewer_hello = await _drain_until(viewer_ws, "hello")
            assert viewer_hello is not None
            assert viewer_hello["role"] == "viewer"
            assert viewer_hello["can_hijack"] is False
            await viewer_ws.send(json.dumps({"type": "hijack_request"}))
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
            await admin_ws.send(json.dumps({"type": "hijack_request"}))
            hijack_state = await _wait_for_hijack_state(admin_ws, expected=True)
            assert hijack_state is not None
            assert hijack_state["hijacked"] is True
