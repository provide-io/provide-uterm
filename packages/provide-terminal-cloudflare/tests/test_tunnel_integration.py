#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Integration tests for the CF tunnel system.

Covers token expiry, timing-safe comparison, enumeration prevention,
TTL clamping, binary frame dispatch in the DO, and KV-loaded worker tokens.
"""

from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from provide.terminal.cloudflare.api._tunnel_api import (
    handle_share_route,
    handle_tunnels,
    resolve_share_context,
)
from provide.terminal.cloudflare.do.session_runtime import SessionRuntime

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _kv_entry(
    tunnel_id: str = "tunnel-abc123",
    share_token: str = "share-tok",  # noqa: S107
    control_token: str = "ctrl-tok",  # noqa: S107
    worker_token: str = "worker-tok",  # noqa: S107
    expires_at: float | None = None,
    **extra: Any,
) -> str:
    entry: dict[str, Any] = {
        "session_id": tunnel_id,
        "display_name": tunnel_id,
        "created_at": time.time(),
        "connector_type": "tunnel:terminal",
        "lifecycle_state": "waiting",
        "input_mode": "open",
        "share_token": share_token,
        "control_token": control_token,
        "worker_token": worker_token,
        "tags": [],
        "visibility": "public",
        "owner": None,
        **extra,
    }
    if expires_at is not None:
        entry["expires_at"] = expires_at
    return json.dumps(entry)


def _mock_env(kv_data: dict[str, str] | None = None) -> MagicMock:
    env = MagicMock()
    kv = MagicMock()
    kv.put = AsyncMock()
    if kv_data is not None:
        kv.get = AsyncMock(side_effect=lambda key: kv_data.get(key))
    else:
        kv.get = AsyncMock(return_value=None)
    env.SESSION_REGISTRY = kv
    return env


def _mock_request(
    url: str = "https://example.com/s/tunnel-abc123?token=share-tok",
    method: str = "GET",
) -> MagicMock:
    req = MagicMock()
    req.url = url
    req.method = method
    req.headers = MagicMock()
    req.headers.get = MagicMock(return_value=None)
    return req


# ---------------------------------------------------------------------------
# 1-2. Token expiry in resolve_share_context
# ---------------------------------------------------------------------------


class TestResolveShareContextExpiry:
    @pytest.mark.asyncio
    async def test_expired_token_returns_none(self) -> None:
        """KV entry with expires_at in the past -> None."""
        expired = time.time() - 3600
        entry = _kv_entry(expires_at=expired)
        env = _mock_env({"session:tunnel-abc123": entry})
        req = _mock_request("https://example.com/s/tunnel-abc123?token=share-tok")

        result = await resolve_share_context(req, env, "tunnel-abc123")
        assert result is None

    @pytest.mark.asyncio
    async def test_valid_future_token_returns_context(self) -> None:
        """KV entry with expires_at in the future -> valid context."""
        future = time.time() + 3600
        entry = _kv_entry(expires_at=future)
        env = _mock_env({"session:tunnel-abc123": entry})
        req = _mock_request("https://example.com/s/tunnel-abc123?token=share-tok")

        result = await resolve_share_context(req, env, "tunnel-abc123")
        assert result == ("session", "viewer")


# ---------------------------------------------------------------------------
# 3. Timing-safe comparison correctness (wrong token -> None)
# ---------------------------------------------------------------------------


class TestTimingSafeComparison:
    @pytest.mark.asyncio
    async def test_wrong_token_returns_none(self) -> None:
        """Wrong token should return None (secrets.compare_digest rejects)."""
        future = time.time() + 3600
        entry = _kv_entry(expires_at=future)
        env = _mock_env({"session:tunnel-abc123": entry})
        req = _mock_request("https://example.com/s/tunnel-abc123?token=wrong-token")

        result = await resolve_share_context(req, env, "tunnel-abc123")
        assert result is None


# ---------------------------------------------------------------------------
# 4-5. Enumeration prevention: handle_share_route returns 404
# ---------------------------------------------------------------------------


class TestEnumerationPrevention:
    @pytest.mark.asyncio
    async def test_invalid_token_returns_404_not_403(self) -> None:
        """Invalid token for existing session -> 404 (not 403)."""
        future = time.time() + 3600
        entry = _kv_entry(expires_at=future)
        env = _mock_env({"session:tunnel-abc123": entry})
        req = _mock_request("https://example.com/s/tunnel-abc123?token=bad-token")
        spa = MagicMock()

        resp = await handle_share_route(req, env, "tunnel-abc123", spa)
        body = json.loads(resp.body)
        assert resp.status == 404
        assert body["error"] == "not_found"

    @pytest.mark.asyncio
    async def test_nonexistent_session_returns_404(self) -> None:
        """Non-existent session -> 404."""
        env = _mock_env({})  # empty KV
        req = _mock_request("https://example.com/s/no-such-id?token=anything")
        spa = MagicMock()

        resp = await handle_share_route(req, env, "no-such-id", spa)
        body = json.loads(resp.body)
        assert resp.status == 404
        assert body["error"] == "not_found"


# ---------------------------------------------------------------------------
# 6-8. Token creation with TTL via handle_tunnels
# ---------------------------------------------------------------------------


class TestHandleTunnelsTTL:
    @pytest.mark.asyncio
    async def test_stores_expires_at(self) -> None:
        """handle_tunnels stores expires_at in KV entry."""
        env = _mock_env()
        req = MagicMock()
        req.method = "POST"
        req.url = "https://example.com/api/tunnels"
        req.json = AsyncMock(return_value={"tunnel_type": "terminal"})
        req.headers = MagicMock()
        req.headers.get = MagicMock(return_value=None)

        before = time.time()
        await handle_tunnels(req, env)
        after = time.time()

        # Check the KV put call
        env.SESSION_REGISTRY.put.assert_called_once()
        stored = json.loads(env.SESSION_REGISTRY.put.call_args[0][1])
        assert "expires_at" in stored
        # Default TTL is 3600s
        assert before + 3600 <= stored["expires_at"] <= after + 3600

    @pytest.mark.asyncio
    async def test_custom_ttl(self) -> None:
        """handle_tunnels with ttl_s in payload stores correct expires_at."""
        env = _mock_env()
        req = MagicMock()
        req.method = "POST"
        req.url = "https://example.com/api/tunnels"
        req.json = AsyncMock(return_value={"tunnel_type": "terminal", "ttl_s": 7200})
        req.headers = MagicMock()
        req.headers.get = MagicMock(return_value=None)

        before = time.time()
        await handle_tunnels(req, env)
        after = time.time()

        stored = json.loads(env.SESSION_REGISTRY.put.call_args[0][1])
        assert before + 7200 <= stored["expires_at"] <= after + 7200

    @pytest.mark.asyncio
    async def test_ttl_clamped_min(self) -> None:
        """TTL below 60 is clamped to 60."""
        env = _mock_env()
        req = MagicMock()
        req.method = "POST"
        req.url = "https://example.com/api/tunnels"
        req.json = AsyncMock(return_value={"tunnel_type": "terminal", "ttl_s": 5})
        req.headers = MagicMock()
        req.headers.get = MagicMock(return_value=None)

        before = time.time()
        await handle_tunnels(req, env)
        after = time.time()

        stored = json.loads(env.SESSION_REGISTRY.put.call_args[0][1])
        assert before + 60 <= stored["expires_at"] <= after + 60

    @pytest.mark.asyncio
    async def test_ttl_clamped_max(self) -> None:
        """TTL above 86400 is clamped to 86400."""
        env = _mock_env()
        req = MagicMock()
        req.method = "POST"
        req.url = "https://example.com/api/tunnels"
        req.json = AsyncMock(return_value={"tunnel_type": "terminal", "ttl_s": 999999})
        req.headers = MagicMock()
        req.headers.get = MagicMock(return_value=None)

        before = time.time()
        await handle_tunnels(req, env)
        after = time.time()

        stored = json.loads(env.SESSION_REGISTRY.put.call_args[0][1])
        assert before + 86400 <= stored["expires_at"] <= after + 86400


# ---------------------------------------------------------------------------
# 9. Session runtime binary frame detection -> handle_tunnel_message
# ---------------------------------------------------------------------------


def _make_ctx(worker_id: str = "tunnel-test") -> SimpleNamespace:
    conn = sqlite3.connect(":memory:")
    return SimpleNamespace(
        storage=SimpleNamespace(
            sql=SimpleNamespace(exec=conn.execute),
            setAlarm=lambda ms: None,
        ),
        id=SimpleNamespace(name=lambda: worker_id),
        getWebSockets=list,
    )


def _make_env(mode: str = "dev", **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(AUTH_MODE=mode, **extra)


class _MockWs:
    def __init__(self, attachment: str = "worker:admin:tunnel-test") -> None:
        self._attachment = attachment
        self.sent: list[str] = []

    def deserializeAttachment(self) -> str:  # noqa: N802
        return self._attachment

    def send(self, data: str) -> None:
        self.sent.append(data)


class TestSessionRuntimeBinaryDispatch:
    @pytest.mark.asyncio
    async def test_binary_worker_message_dispatches_to_tunnel_handler(self) -> None:
        """Binary message from worker role dispatches to handle_tunnel_message."""
        ctx = _make_ctx("tunnel-test")
        env = _make_env()
        rt = SessionRuntime(ctx, env)
        rt.worker_id = "tunnel-test"

        ws = _MockWs("worker:admin:tunnel-test")

        mock_handler = AsyncMock()
        # The lazy import inside webSocketMessage does:
        #   from provide.terminal.cloudflare.api.tunnel_routes import handle_tunnel_message
        # So we patch the function on the tunnel_routes module.
        with patch(
            "provide.terminal.cloudflare.api.tunnel_routes.handle_tunnel_message",
            mock_handler,
        ):
            await rt.webSocketMessage(ws, b"\x01\x00hello")
            mock_handler.assert_called_once()
            call_args = mock_handler.call_args
            assert call_args[0][0] is rt
            assert call_args[0][2] == b"\x01\x00hello"


# ---------------------------------------------------------------------------
# 10. Worker token loaded from KV via _ensure_meta
# ---------------------------------------------------------------------------


class TestEnsureMetaTunnelToken:
    @pytest.mark.asyncio
    async def test_worker_token_loaded_from_kv(self) -> None:
        """_ensure_meta loads worker_token from KV data."""
        ctx = _make_ctx("tunnel-tok-test")
        kv_data = _kv_entry(
            tunnel_id="tunnel-tok-test",
            worker_token="secret-worker-token",
            expires_at=time.time() + 3600,
        )
        env = _make_env(
            SESSION_REGISTRY=MagicMock(
                get=AsyncMock(return_value=kv_data),
            )
        )
        rt = SessionRuntime(ctx, env)
        rt.worker_id = "tunnel-tok-test"

        await rt._ensure_meta()
        assert rt._tunnel_worker_token == "secret-worker-token"

    @pytest.mark.asyncio
    async def test_worker_token_none_when_absent(self) -> None:
        """_ensure_meta leaves _tunnel_worker_token as None when not in KV."""
        ctx = _make_ctx("tunnel-no-tok")
        entry = json.dumps(
            {
                "session_id": "tunnel-no-tok",
                "display_name": "test",
                "connector_type": "shell",
                "created_at": time.time(),
                "tags": [],
                "visibility": "public",
                "owner": None,
            }
        )
        env = _make_env(
            SESSION_REGISTRY=MagicMock(
                get=AsyncMock(return_value=entry),
            )
        )
        rt = SessionRuntime(ctx, env)
        rt.worker_id = "tunnel-no-tok"

        await rt._ensure_meta()
        assert rt._tunnel_worker_token is None


# ---------------------------------------------------------------------------
# 11. Expired worker token — resolve_share_context rejects expired entries
# ---------------------------------------------------------------------------


class TestExpiredWorkerToken:
    @pytest.mark.asyncio
    async def test_expired_entry_rejected_by_resolve_share_context(self) -> None:
        """After KV data has expired expires_at, tokens are treated as expired."""
        expired = time.time() - 100
        entry = _kv_entry(expires_at=expired)
        env = _mock_env({"session:tunnel-abc123": entry})

        # Even with the correct share token, expired entry returns None.
        req = _mock_request("https://example.com/s/tunnel-abc123?token=share-tok")
        result = await resolve_share_context(req, env, "tunnel-abc123")
        assert result is None

    @pytest.mark.asyncio
    async def test_expired_entry_rejected_for_control_token(self) -> None:
        """Expired entry also rejects valid control token."""
        expired = time.time() - 100
        entry = _kv_entry(expires_at=expired)
        env = _mock_env({"session:tunnel-abc123": entry})

        req = _mock_request("https://example.com/s/tunnel-abc123?token=ctrl-tok")
        result = await resolve_share_context(req, env, "tunnel-abc123")
        assert result is None


class TestCookieAuth:
    """Cookie-based share token auth on CF."""

    @pytest.mark.asyncio
    async def test_cookie_share_token_accepted(self) -> None:
        """resolve_share_context accepts share token from cookie."""
        entry = _kv_entry()
        env = _mock_env({"session:tunnel-abc123": entry})
        req = _mock_request("https://example.com/app/session/tunnel-abc123")
        req.headers = MagicMock()
        req.headers.get = MagicMock(
            side_effect=lambda k: "uterm_tunnel_tunnel-abc123=share-tok" if k.lower() == "cookie" else None
        )
        result = await resolve_share_context(req, env, "tunnel-abc123")
        assert result is not None
        assert result[1] == "viewer"

    @pytest.mark.asyncio
    async def test_cookie_control_token_accepted(self) -> None:
        entry = _kv_entry()
        env = _mock_env({"session:tunnel-abc123": entry})
        req = _mock_request("https://example.com/app/operator/tunnel-abc123")
        req.headers = MagicMock()
        req.headers.get = MagicMock(
            side_effect=lambda k: "uterm_tunnel_tunnel-abc123=ctrl-tok" if k.lower() == "cookie" else None
        )
        result = await resolve_share_context(req, env, "tunnel-abc123")
        assert result is not None
        assert result[1] == "operator"

    @pytest.mark.asyncio
    async def test_cookie_wrong_token_rejected(self) -> None:
        entry = _kv_entry()
        env = _mock_env({"session:tunnel-abc123": entry})
        req = _mock_request("https://example.com/app/session/tunnel-abc123")
        req.headers = MagicMock()
        req.headers.get = MagicMock(
            side_effect=lambda k: "uterm_tunnel_tunnel-abc123=wrong" if k.lower() == "cookie" else None
        )
        result = await resolve_share_context(req, env, "tunnel-abc123")
        assert result is None

    @pytest.mark.asyncio
    async def test_query_takes_precedence_over_cookie(self) -> None:
        """When both query param and cookie present, query param wins."""
        entry = _kv_entry()
        env = _mock_env({"session:tunnel-abc123": entry})
        req = _mock_request("https://example.com/app/session/tunnel-abc123?token=share-tok")
        req.headers = MagicMock()
        req.headers.get = MagicMock(
            side_effect=lambda k: "uterm_tunnel_tunnel-abc123=ctrl-tok" if k.lower() == "cookie" else None
        )
        result = await resolve_share_context(req, env, "tunnel-abc123")
        assert result is not None
        assert result[1] == "viewer"  # share-tok from query, not ctrl-tok from cookie
