#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for tunnel routes — binary frame handling in the CF DO."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.tunnel.token_hash import hash_token


class _MockRuntime:
    def __init__(self) -> None:
        self.worker_id = "tunnel-abc123"
        self.lifecycle_state = "stopped"
        self.last_snapshot: dict[str, Any] | None = None
        self.broadcast_worker_frame = AsyncMock()


class TestTunnelAuthz:
    """Ownership enforcement on tunnel management in CF worker.

    Mirrors the FastAPI server's tunnel access control: creating a tunnel
    records the caller as owner and marks the session private; rotate and
    revoke require owner-or-admin; service-token principals (admin) bypass.
    """

    @pytest.mark.asyncio
    async def test_create_tunnel_records_owner_and_private_visibility(self) -> None:
        from provide.uterm.cloudflare.api._tunnel_api import handle_tunnels

        kv = MagicMock()
        kv.put = AsyncMock()
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.method = "POST"
        request.url = "https://example.com/api/tunnels"
        request.json = AsyncMock(return_value={"tunnel_type": "terminal"})

        principal = SimpleNamespace(subject_id="alice", roles=("viewer",))
        resp = await handle_tunnels(request, env, principal)
        assert resp.status == 200
        stored = json.loads(kv.put.call_args[0][1])
        assert stored["owner"] == "alice"
        assert stored["visibility"] == "private"

    @pytest.mark.asyncio
    async def test_create_tunnel_open_mode_keeps_public_ownerless(self) -> None:
        """None principal (dev/none mode) → visibility=public, no owner."""
        from provide.uterm.cloudflare.api._tunnel_api import handle_tunnels

        kv = MagicMock()
        kv.put = AsyncMock()
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.method = "POST"
        request.url = "https://example.com/api/tunnels"
        request.json = AsyncMock(return_value={"tunnel_type": "terminal"})

        resp = await handle_tunnels(request, env, None)
        assert resp.status == 200
        stored = json.loads(kv.put.call_args[0][1])
        assert stored["owner"] is None
        assert stored["visibility"] == "public"

    @pytest.mark.asyncio
    async def test_revoke_non_owner_gets_403(self) -> None:
        """Bob cannot revoke Alice's tunnel."""
        from provide.uterm.cloudflare.api._tunnel_api import handle_tunnel_revoke_tokens

        entry = {
            "session_id": "tunnel-abc",
            "owner": "alice",
            "share_token_hash": hash_token("s"),
            "control_token_hash": hash_token("c"),
        }
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(entry))
        kv.put = AsyncMock()
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        bob = SimpleNamespace(subject_id="bob", roles=("viewer",))
        resp = await handle_tunnel_revoke_tokens(MagicMock(), env, "tunnel-abc", bob)
        assert resp.status == 403
        kv.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_revoke_owner_allowed(self) -> None:
        from provide.uterm.cloudflare.api._tunnel_api import handle_tunnel_revoke_tokens

        entry = {
            "session_id": "tunnel-abc",
            "owner": "alice",
            "share_token_hash": hash_token("s"),
            "control_token_hash": hash_token("c"),
        }
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(entry))
        kv.put = AsyncMock()
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        alice = SimpleNamespace(subject_id="alice", roles=("viewer",))
        resp = await handle_tunnel_revoke_tokens(MagicMock(), env, "tunnel-abc", alice)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_revoke_admin_bypass(self) -> None:
        """An admin principal can revoke any tunnel."""
        from provide.uterm.cloudflare.api._tunnel_api import handle_tunnel_revoke_tokens

        entry = {
            "session_id": "tunnel-abc",
            "owner": "alice",
            "share_token_hash": hash_token("s"),
            "control_token_hash": hash_token("c"),
        }
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(entry))
        kv.put = AsyncMock()
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        admin = SimpleNamespace(subject_id="svc:ops", roles=("admin",))
        resp = await handle_tunnel_revoke_tokens(MagicMock(), env, "tunnel-abc", admin)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_rotate_non_owner_gets_403(self) -> None:
        from provide.uterm.cloudflare.api._tunnel_api import handle_tunnel_rotate_tokens

        entry = {
            "session_id": "tunnel-abc",
            "owner": "alice",
            "worker_token_hash": hash_token("w"),
            "share_token_hash": hash_token("s"),
            "control_token_hash": hash_token("c"),
            "expires_at": time.time() + 100,
        }
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(entry))
        kv.put = AsyncMock()
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.url = "https://example.com/api/tunnels/tunnel-abc/tokens/rotate"

        bob = SimpleNamespace(subject_id="bob", roles=("viewer",))
        resp = await handle_tunnel_rotate_tokens(request, env, "tunnel-abc", bob, ttl_s=3600)
        assert resp.status == 403
        kv.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_rotate_owner_allowed(self) -> None:
        from provide.uterm.cloudflare.api._tunnel_api import handle_tunnel_rotate_tokens

        entry = {
            "session_id": "tunnel-abc",
            "owner": "alice",
            "worker_token_hash": hash_token("w"),
            "share_token_hash": hash_token("s"),
            "control_token_hash": hash_token("c"),
            "expires_at": time.time() + 100,
        }
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(entry))
        kv.put = AsyncMock()
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.url = "https://example.com/api/tunnels/tunnel-abc/tokens/rotate"

        alice = SimpleNamespace(subject_id="alice", roles=("viewer",))
        resp = await handle_tunnel_rotate_tokens(request, env, "tunnel-abc", alice, ttl_s=3600)
        assert resp.status == 200


class TestTunnelSharePageKind:
    """F3: share-page metadata survives the one-time invite URL flow."""

    @pytest.mark.asyncio
    async def test_create_http_tunnel_share_url_points_to_inspect(self) -> None:
        from provide.uterm.cloudflare.api._tunnel_api import handle_tunnels

        kv = MagicMock()
        kv.put = AsyncMock()
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.method = "POST"
        request.url = "https://example.com/api/tunnels"
        request.json = AsyncMock(return_value={"tunnel_type": "http"})

        resp = await handle_tunnels(request, env)
        body = json.loads(resp.body)
        assert "/s/" in body["share_url"]
        assert "invite=" in body["share_url"]
        stored = json.loads(kv.put.call_args[0][1])
        assert stored["share_page"] == "inspect"

    @pytest.mark.asyncio
    async def test_create_terminal_tunnel_share_url_points_to_session(self) -> None:
        from provide.uterm.cloudflare.api._tunnel_api import handle_tunnels

        kv = MagicMock()
        kv.put = AsyncMock()
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.method = "POST"
        request.url = "https://example.com/api/tunnels"
        request.json = AsyncMock(return_value={"tunnel_type": "terminal"})

        resp = await handle_tunnels(request, env)
        body = json.loads(resp.body)
        assert "/s/" in body["share_url"]
        assert "invite=" in body["share_url"]
        stored = json.loads(kv.put.call_args[0][1])
        assert stored["share_page"] == "session"

    @pytest.mark.asyncio
    async def test_rotate_http_tunnel_share_url_points_to_inspect(self) -> None:
        from provide.uterm.cloudflare.api._tunnel_api import handle_tunnel_rotate_tokens

        entry = {
            "session_id": "tunnel-abc",
            "tunnel_type": "http",
            "worker_token_hash": hash_token("w"),
            "share_token_hash": hash_token("s"),
            "control_token_hash": hash_token("c"),
            "expires_at": time.time() + 100,
        }
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(entry))
        kv.put = AsyncMock()
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.url = "https://example.com/api/tunnels/tunnel-abc/tokens/rotate"

        resp = await handle_tunnel_rotate_tokens(request, env, "tunnel-abc", ttl_s=3600)
        body = json.loads(resp.body)
        assert "/s/tunnel-abc?invite=" in body["share_url"]
        stored = json.loads(kv.put.call_args[0][1])
        assert stored["share_page"] == "inspect"


class TestTunnelRevocationBlocksAccess:
    """F2: Revoked tunnels must not grant viewer access via resolve_share_context."""

    @pytest.mark.asyncio
    async def test_revoked_tunnel_returns_none_with_no_token(self) -> None:
        from provide.uterm.cloudflare.api._tunnel_api import resolve_share_context

        session = {"share_token_hash": None, "control_token_hash": None, "revoked": True}
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(session))
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.url = "https://example.com/app/session/tunnel-abc"

        result = await resolve_share_context(request, env, "tunnel-abc")
        assert result is None

    @pytest.mark.asyncio
    async def test_revoke_sets_revoked_flag_in_kv(self) -> None:
        from provide.uterm.cloudflare.api._tunnel_api import handle_tunnel_revoke_tokens

        entry = {
            "session_id": "tunnel-abc",
            "share_token_hash": hash_token("s"),
            "control_token_hash": hash_token("c"),
        }
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(entry))
        kv.put = AsyncMock()
        env = MagicMock()
        env.SESSION_REGISTRY = kv

        await handle_tunnel_revoke_tokens(MagicMock(), env, "tunnel-abc")
        stored = json.loads(kv.put.call_args[0][1])
        assert stored["revoked"] is True

    @pytest.mark.asyncio
    async def test_valid_token_on_revoked_tunnel_is_rejected(self) -> None:
        """Even presenting a valid share_token on a revoked entry returns None."""
        from provide.uterm.cloudflare.api._tunnel_api import resolve_share_context

        session = {
            "share_token_hash": hash_token("valid-tok"),
            "control_token_hash": hash_token("ctrl"),
            "revoked": True,
        }
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(session))
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.url = "https://example.com/app/session/tunnel-abc?token=valid-tok"

        result = await resolve_share_context(request, env, "tunnel-abc")
        assert result is None


class TestTunnelTokenTransportEnforcement:
    """F1: tunnel_token_transport is legacy; tunnel auth is cookie-only."""

    @pytest.mark.asyncio
    async def test_cookie_only_mode_rejects_query_token(self) -> None:
        from provide.uterm.cloudflare.api._tunnel_api import resolve_share_context

        session = {
            "share_token_hash": hash_token("tok"),
            "control_token_hash": hash_token("ctrl"),
            "expires_at": time.time() + 3600,
        }
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(session))
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        # Token is in the query string, but config says cookie-only.
        request = MagicMock()
        request.url = "https://example.com/app/session/tunnel-abc?token=tok"
        config = SimpleNamespace(tunnel_token_transport="cookie", tunnel_ip_binding=False)

        result = await resolve_share_context(request, env, "tunnel-abc", config)
        assert result is None

    @pytest.mark.asyncio
    async def test_cookie_only_mode_accepts_cookie_token(self) -> None:
        from provide.uterm.cloudflare.api._tunnel_api import resolve_share_context

        session = {
            "share_token_hash": hash_token("tok"),
            "control_token_hash": hash_token("ctrl"),
            "expires_at": time.time() + 3600,
        }
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(session))
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.url = "https://example.com/app/session/tunnel-abc"
        request.headers = SimpleNamespace(
            get=lambda k, d=None: "uterm_tunnel_tunnel-abc=tok" if k in ("cookie", "Cookie") else d
        )
        config = SimpleNamespace(tunnel_token_transport="cookie", tunnel_ip_binding=False)

        result = await resolve_share_context(request, env, "tunnel-abc", config)
        assert result == ("session", "viewer")

    @pytest.mark.asyncio
    async def test_legacy_query_mode_still_accepts_cookie_token(self) -> None:
        from provide.uterm.cloudflare.api._tunnel_api import resolve_share_context

        session = {
            "share_token_hash": hash_token("tok"),
            "control_token_hash": hash_token("ctrl"),
            "expires_at": time.time() + 3600,
        }
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(session))
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        # No token in query string; cookie header carries it.
        request.url = "https://example.com/app/session/tunnel-abc"
        request.headers = SimpleNamespace(
            get=lambda k, d=None: "uterm_tunnel_tunnel-abc=tok" if k in ("cookie", "Cookie") else d
        )
        config = SimpleNamespace(tunnel_token_transport="query", tunnel_ip_binding=False)

        result = await resolve_share_context(request, env, "tunnel-abc", config)
        assert result == ("session", "viewer")
