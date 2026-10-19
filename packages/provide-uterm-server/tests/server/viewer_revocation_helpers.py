#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared helpers for the share-viewer revocation suites.

A plain module rather than a conftest: the suites call these with arguments,
and mutmut runs them with ``--import-mode=importlib``, under which a relative
import of a sibling module works and an import of ``tests.server`` does not.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from starlette.websockets import WebSocketDisconnect

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.models import AuthConfig

JWT_KEY = "uterm-test-secret-32-byte-minimum-key"
SESSION = "provide-shell"


def mint_token(sub: str, roles: list[str], *, iat: int | None = None, exp_in: int = 600) -> str:
    """An HS256 token the app built by ``make_app`` accepts."""
    issued = int(time.time()) if iat is None else iat
    return jwt.encode(
        {
            "sub": sub,
            "roles": roles,
            "iss": "provide-uterm",
            "aud": "provide-uterm-server",
            "iat": issued,
            "exp": issued + exp_in,
        },
        key=JWT_KEY,
        algorithm="HS256",
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_app() -> Any:
    """An API-only server app that verifies ``mint_token`` tokens."""
    config = default_server_config()
    config.auth = AuthConfig(
        mode="jwt",
        jwt_public_key_pem=JWT_KEY,
        jwt_algorithms=["HS256"],
        worker_bearer_token=mint_token("runtime-worker", ["admin"]),
        clock_skew_seconds=30,
    )
    return create_server_app(config, api_only=True)


def wait_for(browser: Any, frame_type: str) -> dict[str, Any]:
    """The next frame of ``frame_type``, skipping any others."""
    while True:
        frame = browser.receive_json()
        if frame.get("type") == frame_type:
            return frame


def close_of(browser: Any) -> WebSocketDisconnect:
    """Wait for the server to close ``browser`` and return how it closed it."""
    # A ping first, so a socket that was wrongly left open answers it and the
    # test fails on the pong instead of waiting forever for a close.
    browser.send_json({"type": "ping"})
    with pytest.raises(WebSocketDisconnect) as caught:
        while True:
            assert browser.receive_json().get("type") != "pong", "socket was not closed"
    return caught.value
