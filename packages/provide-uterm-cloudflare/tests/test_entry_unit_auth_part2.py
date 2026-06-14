#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Entry profile and principal-auth unit tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import jwt
from provide.uterm.cloudflare.config import CloudflareConfig
from provide.uterm.cloudflare.entry import Default
from provide.uterm.cloudflare.entry.auth import _resolve_principal_id


def _make_default(env_attrs: dict | None = None) -> Default:
    attrs: dict = {
        "AUTH_MODE": "jwt",
        "JWT_ALGORITHMS": "HS256",
        "JWT_PUBLIC_KEY_PEM": "test-secret-key-32-bytes-minimum!",
        "WORKER_BEARER_TOKEN": "test-worker-token-padded-to-32xyz",
    }
    force_dev = not (env_attrs and env_attrs.get("AUTH_MODE") == "jwt")
    if env_attrs:
        attrs.update(env_attrs)
    env = SimpleNamespace(**attrs)
    entry = Default(env)
    entry._config = CloudflareConfig.from_env(env)
    if force_dev:
        entry._config.jwt.mode = "dev"
    return entry


class _FakeKV:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def put(self, key: str, value: str) -> None:
        self._data[key] = value

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def list(self, prefix: str = "") -> list[dict[str, str]]:
        return [{"name": k} for k in self._data if k.startswith(prefix)]


def _jwt_config(public_key: str = "k") -> CloudflareConfig:
    return CloudflareConfig.from_env(
        SimpleNamespace(
            AUTH_MODE="jwt",
            JWT_ALGORITHMS="HS256",
            JWT_PUBLIC_KEY_PEM=public_key,
            WORKER_BEARER_TOKEN="test-worker-token-padded-to-32xyz",
        )
    )


async def test_default_fetch_profiles_list_dev_mode() -> None:
    """GET /api/profiles in dev mode uses 'dev-user' principal."""
    kv = _FakeKV()
    entry = _make_default({"SESSION_REGISTRY": kv})

    resp = await entry.fetch(SimpleNamespace(url="https://x/api/profiles", method="GET", headers={}))

    assert resp.status == 200
    assert json.loads(resp.body) == []


async def test_default_fetch_profiles_create_dev_mode() -> None:
    """POST /api/profiles in dev mode creates profile owned by 'dev-user'."""
    kv = _FakeKV()
    entry = _make_default({"SESSION_REGISTRY": kv})

    async def _json() -> dict:
        return {"name": "Test", "connector_type": "ssh"}

    req = SimpleNamespace(url="https://x/api/profiles", method="POST", headers={}, json=_json)
    resp = await entry.fetch(req)

    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["owner"] == "dev-user"
    assert body["name"] == "Test"


async def test_default_fetch_profiles_jwt_mode_no_token() -> None:
    """GET /api/profiles in jwt mode with no token returns 401."""
    kv = _FakeKV()
    entry = _make_default(
        {
            "AUTH_MODE": "jwt",
            "JWT_ALGORITHMS": "HS256",
            "JWT_PUBLIC_KEY_PEM": "test-key",
            "WORKER_BEARER_TOKEN": "test-worker-token-padded-to-32xyz",
            "SESSION_REGISTRY": kv,
        }
    )

    resp = await entry.fetch(SimpleNamespace(url="https://x/api/profiles", method="GET", headers={}))

    assert resp.status == 401


async def test_resolve_principal_id_no_token() -> None:
    """_resolve_principal_id with no token returns 'anonymous'."""
    result = await _resolve_principal_id(
        SimpleNamespace(headers=SimpleNamespace(get=lambda _k, d=None: d)), _jwt_config()
    )

    assert result == "anonymous"


async def test_resolve_principal_id_invalid_token() -> None:
    """_resolve_principal_id with invalid JWT returns 'anonymous'."""
    req = SimpleNamespace(
        headers=SimpleNamespace(get=lambda k, d=None: "Bearer invalid-token" if k.lower() == "authorization" else d)
    )

    result = await _resolve_principal_id(req, _jwt_config())

    assert result == "anonymous"


async def test_resolve_principal_id_valid_token() -> None:
    """_resolve_principal_id with valid JWT returns subject."""
    secret = "a-sufficiently-long-secret-key-for-hs256"  # pragma: allowlist secret
    token = jwt.encode({"sub": "alice", "exp": 9999999999}, secret, algorithm="HS256")
    req = SimpleNamespace(
        headers=SimpleNamespace(get=lambda k, d=None: f"Bearer {token}" if k.lower() == "authorization" else d)
    )

    result = await _resolve_principal_id(req, _jwt_config(secret))

    assert result == "alice"


async def test_resolve_principal_id_cf_access_email_header() -> None:
    """CF Access authenticated-user-email header resolves to the user's email."""
    req = SimpleNamespace(
        headers=SimpleNamespace(
            get=lambda k, d=None: "alice@example.com" if k.lower() == "cf-access-authenticated-user-email" else d
        )
    )

    result = await _resolve_principal_id(req, _jwt_config())

    assert result == "alice@example.com"
