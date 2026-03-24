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

from provide.terminal.control_stream import ControlChunk, ControlStreamDecoder


def _decode_control_frames(messages: list[str]) -> list[dict]:
    """Decode a list of control-stream-encoded messages into plain dicts."""
    result = []
    for raw in messages:
        decoder = ControlStreamDecoder()
        events = decoder.feed(raw)
        events.extend(decoder.finish())
        result.extend(e.control for e in events if isinstance(e, ControlChunk))
    return result


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
        pytest.raises(JwtValidationError, match="failed to verify token"),
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
# do/session_runtime.py — browser_role_for_request: exception handling (lines 160-166)
# ---------------------------------------------------------------------------


async def test_browser_role_for_request_jwt_validation_error_returns_viewer() -> None:
    """JwtValidationError (bad/expired token) falls back to viewer — not a server fault."""
    from provide_terminal_cloudflare.auth.jwt import JwtValidationError

    rt = _make_runtime(mode="jwt")

    token = _make_token(sub="u1", roles=["admin"])

    class _Req:
        headers = SimpleNamespace(get=lambda name, default=None: f"Bearer {token}")
        url = "http://localhost/"

    with patch(
        "provide_terminal_cloudflare.do.session_runtime.decode_jwt",
        new=AsyncMock(side_effect=JwtValidationError("bad token")),
    ):
        role = await rt.browser_role_for_request(_Req())

    assert role == "viewer"


async def test_browser_role_for_request_network_error_propagates() -> None:
    """Non-JwtValidationError exceptions (e.g. JWKS network failure) propagate instead of
    silently downgrading the caller to viewer and returning 403 to a legitimate admin."""
    rt = _make_runtime(mode="jwt")

    token = _make_token(sub="u1", roles=["admin"])

    class _Req:
        headers = SimpleNamespace(get=lambda name, default=None: f"Bearer {token}")
        url = "http://localhost/"

    with (
        patch(
            "provide_terminal_cloudflare.do.session_runtime.decode_jwt",
            new=AsyncMock(side_effect=RuntimeError("JWKS network error")),
        ),
        pytest.raises(RuntimeError, match="JWKS network error"),
    ):
        await rt.browser_role_for_request(_Req())


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

    sent = _decode_control_frames(ws.sent)
    types = [m["type"] for m in sent]
    assert "hello" in types
    assert "snapshot" in types
