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


class TestTunnelIpBinding:
    """F1: tunnel_ip_binding config rejects tokens from mismatched IPs."""

    @pytest.mark.asyncio
    async def test_ip_match_allowed(self) -> None:
        from provide.uterm.cloudflare.api._tunnel_api import resolve_share_context

        session = {
            "share_token_hash": hash_token("tok"),
            "control_token_hash": hash_token("ctrl"),
            "issued_ip": "1.2.3.4",
            "expires_at": time.time() + 3600,
        }
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(session))
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.url = "https://example.com/app/session/tunnel-abc"
        request.headers = SimpleNamespace(
            get=lambda k, d=None: (
                "1.2.3.4" if "Connecting-IP" in k else "uterm_tunnel_tunnel-abc=tok" if k in ("cookie", "Cookie") else d
            )
        )
        config = SimpleNamespace(tunnel_token_transport="both", tunnel_ip_binding=True)

        result = await resolve_share_context(request, env, "tunnel-abc", config)
        assert result == ("session", "viewer")

    @pytest.mark.asyncio
    async def test_ip_mismatch_rejected(self) -> None:
        from provide.uterm.cloudflare.api._tunnel_api import resolve_share_context

        session = {
            "share_token_hash": hash_token("tok"),
            "control_token_hash": hash_token("ctrl"),
            "issued_ip": "1.2.3.4",
            "expires_at": time.time() + 3600,
        }
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(session))
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.url = "https://example.com/app/session/tunnel-abc"
        request.headers = SimpleNamespace(
            get=lambda k, d=None: (
                "9.9.9.9" if "Connecting-IP" in k else "uterm_tunnel_tunnel-abc=tok" if k in ("cookie", "Cookie") else d
            )
        )
        config = SimpleNamespace(tunnel_token_transport="both", tunnel_ip_binding=True)

        result = await resolve_share_context(request, env, "tunnel-abc", config)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_issued_ip_skips_binding_check(self) -> None:
        """Empty issued_ip means binding was not configured at create time — allow."""
        from provide.uterm.cloudflare.api._tunnel_api import resolve_share_context

        session = {
            "share_token_hash": hash_token("tok"),
            "control_token_hash": hash_token("ctrl"),
            "issued_ip": "",
            "expires_at": time.time() + 3600,
        }
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(session))
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.url = "https://example.com/app/session/tunnel-abc"
        request.headers = SimpleNamespace(
            get=lambda k, d=None: (
                "9.9.9.9" if "Connecting-IP" in k else "uterm_tunnel_tunnel-abc=tok" if k in ("cookie", "Cookie") else d
            )
        )
        config = SimpleNamespace(tunnel_token_transport="both", tunnel_ip_binding=True)

        result = await resolve_share_context(request, env, "tunnel-abc", config)
        assert result == ("session", "viewer")

    @pytest.mark.asyncio
    async def test_ip_binding_headers_get_exception_treats_as_no_client_ip(self) -> None:
        """Exception from headers.get in IP binding silently caught; empty client_ip != issued_ip → rejected."""
        from provide.uterm.cloudflare.api._tunnel_api import resolve_share_context

        session = {
            "share_token_hash": hash_token("tok"),
            "control_token_hash": hash_token("ctrl"),
            "issued_ip": "1.2.3.4",
            "expires_at": time.time() + 3600,
        }
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(session))
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.url = "https://example.com/app/session/tunnel-abc?token=tok"

        class _BadHeaders:
            def get(self, k, d=None):
                raise RuntimeError("no headers")

        request.headers = _BadHeaders()
        config = SimpleNamespace(tunnel_token_transport="both", tunnel_ip_binding=True)

        result = await resolve_share_context(request, env, "tunnel-abc", config)
        assert result is None
