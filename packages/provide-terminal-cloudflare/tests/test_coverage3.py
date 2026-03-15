#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Targeted coverage tests for previously uncovered branches."""

from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from provide_terminal_cloudflare.auth.jwt import JwtValidationError, decode_jwt, resolve_role
from provide_terminal_cloudflare.config import JwtConfig
from provide_terminal_cloudflare.do.session_runtime import SessionRuntime

_KEY = "test-secret-key-32-bytes-minimum!"


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_session_runtime_unit.py)
# ---------------------------------------------------------------------------


def _make_ctx(worker_id: str = "test-worker"):
    conn = sqlite3.connect(":memory:")
    return SimpleNamespace(
        storage=SimpleNamespace(
            sql=SimpleNamespace(exec=conn.execute),
            setAlarm=lambda ms: None,
        ),
        id=SimpleNamespace(name=lambda: worker_id),
        getWebSockets=list,
    )


def _make_env(mode: str = "dev", **extra) -> SimpleNamespace:
    env = SimpleNamespace(AUTH_MODE=mode, **extra)
    if mode == "jwt":
        env.JWT_ALGORITHMS = "HS256"
        env.JWT_PUBLIC_KEY_PEM = _KEY
        if not hasattr(env, "WORKER_BEARER_TOKEN"):
            env.WORKER_BEARER_TOKEN = "test-worker-token"
    return env


def _make_runtime(worker_id: str = "test-worker", mode: str = "dev") -> SessionRuntime:
    ctx = _make_ctx(worker_id)
    env = _make_env(mode)
    return SessionRuntime(ctx, env)


def _make_token(sub: str = "user", roles: list[str] | None = None) -> str:
    now = int(time.time())
    payload: dict = {"sub": sub, "iat": now, "exp": now + 600}
    if roles:
        payload["roles"] = roles
    return jwt.encode(payload, _KEY, algorithm="HS256")


class _MockWs:
    def __init__(self, attachment: object = None) -> None:
        self._attachment = attachment
        self.sent: list[str] = []

    def deserializeAttachment(self) -> object:  # noqa: N802
        return self._attachment

    def send(self, data: str) -> None:
        self.sent.append(data)


class _AsyncWs(_MockWs):
    async def send(self, data: str) -> None:  # type: ignore[override]
        self.sent.append(data)


# ---------------------------------------------------------------------------
# auth/jwt.py — _fetch_jwks urllib fallback (lines 37-42)
# ---------------------------------------------------------------------------


async def test_fetch_jwks_urllib_fallback() -> None:
    """_fetch_jwks falls back to urllib when js.fetch is unavailable."""
    from provide_terminal_cloudflare.auth.jwt import _fetch_jwks

    fake_keys: dict = {"keys": []}
    encoded = json.dumps(fake_keys).encode()

    mock_resp = MagicMock()
    mock_resp.read.return_value = encoded
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = await _fetch_jwks("https://example.com/.well-known/jwks.json")

    assert result == fake_keys


# ---------------------------------------------------------------------------
# auth/jwt.py — decode_jwt in dev/none mode (line 74)
# ---------------------------------------------------------------------------


async def test_decode_jwt_dev_mode_returns_dev_principal() -> None:
    config = JwtConfig(mode="dev", public_key_pem="any")
    principal = await decode_jwt("ignored-token", config)
    assert principal.subject_id == "dev"
    assert resolve_role(principal) == "admin"


async def test_decode_jwt_none_mode_returns_dev_principal() -> None:
    config = JwtConfig(mode="none", public_key_pem="any")
    principal = await decode_jwt("ignored-token", config)
    assert principal.subject_id == "dev"


# ---------------------------------------------------------------------------
# auth/jwt.py — unexpected _resolve_signing_key error wrapping (lines 82-83)
# ---------------------------------------------------------------------------


async def test_decode_jwt_unexpected_signing_key_error_wrapped() -> None:
    """Non-JwtValidationError from _resolve_signing_key is wrapped."""
    config = JwtConfig(
        mode="jwt",
        public_key_pem="secret",
        algorithms=("HS256",),
    )
    token = jwt.encode({"sub": "u1", "exp": int(time.time()) + 600}, "secret", algorithm="HS256")

    with (
        patch(
            "provide_terminal_cloudflare.auth.jwt._resolve_signing_key",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        pytest.raises(JwtValidationError, match="failed to resolve signing key"),
    ):
        await decode_jwt(token, config)


# ---------------------------------------------------------------------------
# auth/jwt.py — JWKS no matching key (line 69)
# ---------------------------------------------------------------------------


async def test_jwks_no_matching_key_raises() -> None:
    """When no JWKS key matches kid, JwtValidationError is raised."""
    from provide_terminal_cloudflare.auth.jwt import _resolve_signing_key

    empty_jwks = {"keys": []}

    config = JwtConfig(
        mode="jwt",
        jwks_url="https://example.com/.well-known/jwks.json",
        algorithms=("RS256",),
    )
    # Token with a kid that won't match
    token = jwt.encode(
        {"sub": "u1", "exp": int(time.time()) + 600},
        "dummy-key",
        algorithm="HS256",
        headers={"kid": "missing-kid"},
    )

    with (
        patch("provide_terminal_cloudflare.auth.jwt._fetch_jwks", new=AsyncMock(return_value=empty_jwks)),
        pytest.raises(JwtValidationError, match="no matching key"),
    ):
        await _resolve_signing_key(token, config)


# ---------------------------------------------------------------------------
# auth/jwt.py — JWKS no-kid algorithm matching (lines 60-65)
# ---------------------------------------------------------------------------


async def test_jwks_no_kid_matches_by_algorithm() -> None:
    """When JWT has no kid, JWKS key matching falls back to algorithm."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    now = int(time.time())
    token = jwt.encode(
        {"sub": "u1", "exp": now + 600},
        private_key,
        algorithm="RS256",
        # No kid in header
    )

    config = JwtConfig(
        mode="jwt",
        jwks_url="https://example.com/.well-known/jwks.json",
        algorithms=("RS256",),
    )

    # Build a minimal mock key with algorithm_name
    mock_key = MagicMock()
    mock_key.key_id = None
    mock_key.algorithm_name = "RS256"
    mock_key.key = public_key

    from provide_terminal_cloudflare.auth.jwt import _resolve_signing_key

    with (
        patch("provide_terminal_cloudflare.auth.jwt._fetch_jwks", new=AsyncMock(return_value={})),
        patch("jwt.PyJWKSet.from_dict", return_value=MagicMock(keys=[mock_key])),
        patch("jwt.get_unverified_header", return_value={"alg": "RS256"}),
    ):
        key = await _resolve_signing_key(token, config)
    assert key is public_key


# ---------------------------------------------------------------------------
# do/session_runtime.py — _extract_token: headers.get() raises (lines 105-106)
# ---------------------------------------------------------------------------


def test_extract_token_headers_get_raises_returns_empty() -> None:
    """If request.headers.get raises, auth_header falls back to empty string."""
    rt = _make_runtime(mode="dev")

    class _BadHeaders:
        def get(self, name: str, default=None):
            raise RuntimeError("headers broken")

    class _BadRequest:
        headers = _BadHeaders()
        url = "http://localhost/"

    # With no bearer header and query token disabled:
    rt.config.jwt.allow_query_token = False
    result = rt._extract_token(_BadRequest())
    assert result is None


# ---------------------------------------------------------------------------
# do/session_runtime.py — _extract_token: URL parse fails (lines 118-119)
# ---------------------------------------------------------------------------


def test_extract_token_url_parse_raises_returns_none() -> None:
    """If URL parsing for query token raises, _extract_token returns None."""
    rt = _make_runtime(mode="dev")
    rt.config.jwt.allow_query_token = True

    class _BadUrl:
        def __str__(self):
            raise RuntimeError("bad url")

    class _BadRequest:
        headers = SimpleNamespace(get=lambda name, default=None: None)
        url = _BadUrl()

    result = rt._extract_token(_BadRequest())
    assert result is None


# ---------------------------------------------------------------------------
# do/session_runtime.py — browser_role_for_request: exception → "viewer" (lines 160-164)
# ---------------------------------------------------------------------------


async def test_browser_role_for_request_exception_returns_viewer() -> None:
    """If decode_jwt raises unexpectedly in jwt mode, browser_role falls back to viewer."""
    rt = _make_runtime(mode="jwt")

    token = _make_token(sub="u1", roles=["admin"])

    class _Req:
        headers = SimpleNamespace(get=lambda name, default=None: f"Bearer {token}")
        url = "http://localhost/"

    with patch(
        "provide_terminal_cloudflare.do.session_runtime.decode_jwt",
        new=AsyncMock(side_effect=RuntimeError("unexpected")),
    ):
        role = await rt.browser_role_for_request(_Req())

    assert role == "viewer"


# ---------------------------------------------------------------------------
# do/session_runtime.py — webSocketOpen browser with existing last_snapshot (line 308)
# ---------------------------------------------------------------------------


async def test_websocket_open_browser_sends_last_snapshot() -> None:
    """webSocketOpen sends last_snapshot to browser when one exists."""
    rt = _make_runtime()
    snapshot = {"type": "snapshot", "screen": "hello", "ts": time.time()}
    rt.last_snapshot = snapshot

    ws = _AsyncWs(attachment="browser:admin:test-worker")
    await rt.webSocketOpen(ws)

    sent = [json.loads(m) for m in ws.sent]
    types = [m["type"] for m in sent]
    assert "hello" in types
    assert "snapshot" in types


# ---------------------------------------------------------------------------
# do/session_runtime.py — broadcast_hijack_state: send failure pops browser (lines 398-400)
# ---------------------------------------------------------------------------


async def test_broadcast_hijack_state_removes_failed_socket() -> None:
    """broadcast_hijack_state removes browser sockets whose send raises."""
    rt = _make_runtime()

    class _FailWs(_AsyncWs):
        async def send(self, data: str) -> None:
            raise RuntimeError("send failed")

    fail_ws = _FailWs(attachment="browser:admin:test-worker")
    ws_id = rt.ws_key(fail_ws)
    rt.browser_sockets[ws_id] = fail_ws

    await rt.broadcast_hijack_state()

    assert ws_id not in rt.browser_sockets


# ---------------------------------------------------------------------------
# do/session_runtime.py — broadcast_worker_frame: raw send failure pops raw socket (lines 460-461)
# ---------------------------------------------------------------------------


async def test_broadcast_worker_frame_removes_failed_raw_socket() -> None:
    """broadcast_worker_frame removes raw sockets whose _send_text raises."""
    rt = _make_runtime()

    class _FailRaw(_AsyncWs):
        async def send(self, data: str) -> None:
            raise RuntimeError("raw send failed")

    fail_ws = _FailRaw(attachment="raw:admin:test-worker")
    ws_id = rt.ws_key(fail_ws)
    rt.raw_sockets[ws_id] = fail_ws

    payload = {"type": "term", "data": "hello", "ts": time.time()}
    await rt.broadcast_worker_frame(payload)

    assert ws_id not in rt.raw_sockets


# ---------------------------------------------------------------------------
# auth/jwt.py — _fetch_jwks cache hit (lines 44-46)
# ---------------------------------------------------------------------------


async def test_fetch_jwks_cache_hit() -> None:
    """_fetch_jwks returns cached value without a network call on second call."""
    from unittest.mock import MagicMock

    from provide_terminal_cloudflare.auth import jwt as jwt_module
    from provide_terminal_cloudflare.auth.jwt import _fetch_jwks

    fake_keys: dict = {"keys": [{"kty": "EC"}]}
    encoded = json.dumps(fake_keys).encode()

    call_count = 0

    mock_resp = MagicMock()
    mock_resp.read.return_value = encoded
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    def _counting_urlopen(req):
        nonlocal call_count
        call_count += 1
        return mock_resp

    url = "https://example.com/.well-known/jwks-cache-test.json"
    # Clear any existing cache entry
    jwt_module._JWKS_CACHE.pop(url, None)

    with patch("urllib.request.urlopen", side_effect=_counting_urlopen):
        result1 = await _fetch_jwks(url)
        result2 = await _fetch_jwks(url)

    # Network called only once; second call uses cache
    assert call_count == 1
    assert result1 == fake_keys
    assert result2 == fake_keys


# ---------------------------------------------------------------------------
# auth/jwt.py — _resolve_signing_key: no jwks_url and no public_key_pem (line 92)
# ---------------------------------------------------------------------------


async def test_resolve_signing_key_no_config_raises() -> None:
    """Line 92: no jwks_url and no public_key_pem → JwtValidationError."""
    from provide_terminal_cloudflare.auth.jwt import JwtValidationError, _resolve_signing_key

    config = JwtConfig(mode="jwt", public_key_pem=None, jwks_url=None)
    token = jwt.encode({"sub": "u", "exp": int(time.time()) + 600}, "k", algorithm="HS256")
    with pytest.raises(JwtValidationError, match="must be configured"):
        await _resolve_signing_key(token, config)


# ---------------------------------------------------------------------------
# auth/jwt.py — _resolve_signing_key: kid matches a key (line 83)
# ---------------------------------------------------------------------------


async def test_resolve_signing_key_kid_matches() -> None:
    """Line 83: JWT kid matches a key in JWKS → return that key."""
    from provide_terminal_cloudflare.auth.jwt import _resolve_signing_key

    sentinel_key = object()
    mock_jwks_key = MagicMock()
    mock_jwks_key.key_id = "my-kid"
    mock_jwks_key.key = sentinel_key

    config = JwtConfig(
        mode="jwt",
        jwks_url="https://example.com/.well-known/jwks.json",
        algorithms=("RS256",),
    )
    token = jwt.encode(
        {"sub": "u", "exp": int(time.time()) + 600},
        "dummy",
        algorithm="HS256",
        headers={"kid": "my-kid"},
    )

    with (
        patch("provide_terminal_cloudflare.auth.jwt._fetch_jwks", new=AsyncMock(return_value={})),
        patch("jwt.PyJWKSet.from_dict", return_value=MagicMock(keys=[mock_jwks_key])),
    ):
        key = await _resolve_signing_key(token, config)

    assert key is sentinel_key


# ---------------------------------------------------------------------------
# config.py — invalid mode defaults to "jwt" (line 79)
# ---------------------------------------------------------------------------


def test_config_invalid_mode_defaults_to_jwt() -> None:
    """Line 79: AUTH_MODE with unrecognised value → silently defaults to 'jwt'."""
    from provide_terminal_cloudflare.config import CloudflareConfig

    env = SimpleNamespace(AUTH_MODE="invalid_mode", WORKER_BEARER_TOKEN="t")
    config = CloudflareConfig.from_env(env)
    assert config.jwt.mode == "jwt"


# ---------------------------------------------------------------------------
# do/session_runtime.py — _extract_token: CF_Authorization cookie path
# ---------------------------------------------------------------------------


def test_extract_token_cf_authorization_cookie_returned() -> None:
    """_extract_token returns token from CF_Authorization cookie when present."""
    rt = _make_runtime(mode="jwt")
    token = _make_token()

    def _get_header(name: str, default=None):
        if name == "Cookie":
            return f"session=abc; CF_Authorization={token}; extra=x"
        return None

    req = SimpleNamespace(headers=SimpleNamespace(get=_get_header), url="http://localhost/")
    rt.config.jwt.allow_query_token = False
    result = rt._extract_token(req)
    assert result == token
