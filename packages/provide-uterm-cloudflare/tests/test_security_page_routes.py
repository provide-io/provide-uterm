from __future__ import annotations

import json
import time
from types import SimpleNamespace

import jwt
import pytest
from provide.uterm.cloudflare.config import CloudflareConfig
from provide.uterm.cloudflare.entry import Default


class _Req:
    def __init__(self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None):
        self.url = url
        self.method = method
        self.headers = headers or {}


def _jwt_env(public_key: str = "test-secret-key-32-bytes-minimum!") -> SimpleNamespace:
    return SimpleNamespace(
        AUTH_MODE="jwt",
        JWT_ALGORITHMS="HS256",
        JWT_PUBLIC_KEY_PEM=public_key,
        WORKER_BEARER_TOKEN="test-worker-token-padded-to-32xyz",
        SESSION_RUNTIME=None,
        SESSION_REGISTRY=None,
    )


def _worker(env: SimpleNamespace, *, dev_mode: bool = False) -> Default:
    worker = object.__new__(Default)
    worker.env = env
    worker._config = CloudflareConfig.from_env(env)
    if dev_mode:
        worker._config.jwt.mode = "dev"
    return worker


@pytest.mark.asyncio
async def test_page_routes_require_jwt_in_jwt_mode() -> None:
    """In JWT mode, /app must return 401 when no token is provided."""
    worker = _worker(_jwt_env())
    resp = await worker.fetch(_Req("https://example.invalid/app"))

    assert resp.status == 401
    body = json.loads(resp.body)
    assert body["error"] == "authentication required"


@pytest.mark.asyncio
async def test_page_routes_accessible_in_dev_mode() -> None:
    """In dev mode, /app must return 200 (no auth required)."""
    worker = _worker(_jwt_env(), dev_mode=True)
    resp = await worker.fetch(_Req("https://example.invalid/app"))

    assert resp.status != 401


@pytest.mark.asyncio
async def test_page_routes_invalid_jwt_returns_401() -> None:
    """In JWT mode, /app with an invalid token returns 401 with 'invalid token'."""
    worker = _worker(_jwt_env())
    req = _Req(
        "https://example.invalid/app",
        headers={"Authorization": "Bearer invalid.token.here"},
    )

    resp = await worker.fetch(req)

    assert resp.status == 401
    body = json.loads(resp.body)
    assert body["error"] == "invalid token"
    assert "detail" in body
    assert body["detail"] != "None"


@pytest.mark.asyncio
async def test_page_routes_valid_jwt_returns_non_401() -> None:
    """In JWT mode, /app with a valid token must NOT return 401."""
    signing_key = "test-secret-key-32-bytes-minimum!"
    now = int(time.time())
    token = jwt.encode({"sub": "u1", "exp": now + 600}, signing_key, algorithm="HS256")
    worker = _worker(_jwt_env(signing_key))
    req = _Req(
        "https://example.invalid/app",
        headers={"Authorization": f"Bearer {token}"},
    )

    resp = await worker.fetch(req)

    assert resp.status != 401


@pytest.mark.asyncio
async def test_root_page_requires_jwt_in_jwt_mode() -> None:
    """In JWT mode, / must return 401 when no token is provided."""
    worker = _worker(_jwt_env())
    resp = await worker.fetch(_Req("https://example.invalid/"))

    assert resp.status == 401


@pytest.mark.asyncio
async def test_assets_accessible_without_jwt() -> None:
    """Static assets (/assets/*.js) must be accessible even in JWT mode."""
    worker = _worker(_jwt_env())
    resp = await worker.fetch(_Req("https://example.invalid/assets/hijack.js"))

    assert resp.status != 401
