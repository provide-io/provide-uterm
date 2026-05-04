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
from provide.terminal.cloudflare.api.tunnel_routes import (
    decode_tunnel_frame,
    encode_tunnel_control,
    encode_tunnel_input,
    handle_tunnel_message,
    is_tunnel_message,
)


class _MockRuntime:
    def __init__(self) -> None:
        self.worker_id = "tunnel-abc123"
        self.lifecycle_state = "stopped"
        self.last_snapshot: dict[str, Any] | None = None
        self.broadcast_worker_frame = AsyncMock()


class TestIsTunnelMessage:
    def test_bytes(self) -> None:
        assert is_tunnel_message(b"\x01\x00hello") is True

    def test_bytearray(self) -> None:
        assert is_tunnel_message(bytearray(b"\x01\x00")) is True

    def test_memoryview(self) -> None:
        assert is_tunnel_message(memoryview(b"\x01\x00")) is True

    def test_string(self) -> None:
        assert is_tunnel_message('{"type":"input"}') is False

    def test_none(self) -> None:
        assert is_tunnel_message(None) is False


class TestDecodeTunnelFrame:
    def test_data_frame(self) -> None:
        ch, flags, payload = decode_tunnel_frame(b"\x01\x00hello")
        assert ch == 1 and flags == 0 and payload == b"hello"

    def test_control_frame(self) -> None:
        ch, flags, payload = decode_tunnel_frame(b'\x00\x00{"type":"open"}')
        assert ch == 0 and payload == b'{"type":"open"}'

    def test_eof_frame(self) -> None:
        ch, flags, payload = decode_tunnel_frame(b"\x01\x01")
        assert ch == 1 and flags == 1 and payload == b""

    def test_too_short(self) -> None:
        ch, _, _ = decode_tunnel_frame(b"\x01")
        assert ch == -1

    def test_empty(self) -> None:
        ch, _, _ = decode_tunnel_frame(b"")
        assert ch == -1


class TestEncodeTunnelInput:
    def test_basic(self) -> None:
        data = encode_tunnel_input("ls\n")
        assert data[0] == 1 and data[1] == 0 and data[2:] == b"ls\n"

    def test_custom_channel(self) -> None:
        assert encode_tunnel_input("hello", channel=2)[0] == 2


class TestEncodeTunnelControl:
    def test_basic(self) -> None:
        data = encode_tunnel_control({"type": "input", "data": "ls"})
        assert data[0] == 0 and data[1] == 0
        obj = json.loads(data[2:])
        assert obj["type"] == "input" and obj["data"] == "ls"


class TestHandleTunnelMessage:
    @pytest.mark.asyncio
    async def test_data_frame_broadcasts(self) -> None:
        rt = _MockRuntime()
        await handle_tunnel_message(rt, MagicMock(), b"\x01\x00hello world")
        rt.broadcast_worker_frame.assert_called_once()
        frame = rt.broadcast_worker_frame.call_args[0][0]
        assert frame["type"] == "term" and frame["data"] == "hello world"

    @pytest.mark.asyncio
    async def test_data_frame_creates_snapshot(self) -> None:
        rt = _MockRuntime()
        await handle_tunnel_message(rt, MagicMock(), b"\x01\x00first output")
        assert rt.last_snapshot is not None and "first output" in rt.last_snapshot["screen"]

    @pytest.mark.asyncio
    async def test_data_frame_appends_to_snapshot(self) -> None:
        rt = _MockRuntime()
        rt.last_snapshot = {"type": "snapshot", "screen": "existing ", "ts": time.time()}
        await handle_tunnel_message(rt, MagicMock(), b"\x01\x00more data")
        assert "existing more data" in rt.last_snapshot["screen"]

    @pytest.mark.asyncio
    async def test_snapshot_truncates_at_32k(self) -> None:
        rt = _MockRuntime()
        rt.last_snapshot = {"type": "snapshot", "screen": "x" * 40000, "ts": time.time()}
        await handle_tunnel_message(rt, MagicMock(), b"\x01\x00end")
        assert len(rt.last_snapshot["screen"]) <= 32768

    @pytest.mark.asyncio
    async def test_eof_frame_does_not_broadcast(self) -> None:
        rt = _MockRuntime()
        await handle_tunnel_message(rt, MagicMock(), b"\x01\x01")
        rt.broadcast_worker_frame.assert_not_called()

    @pytest.mark.asyncio
    async def test_control_open_sets_running(self) -> None:
        rt = _MockRuntime()
        ctrl = json.dumps({"type": "open", "tunnel_type": "terminal", "term_size": [80, 24]}).encode()
        await handle_tunnel_message(rt, MagicMock(), b"\x00\x00" + ctrl)
        assert rt.lifecycle_state == "running"

    @pytest.mark.asyncio
    async def test_control_resize(self) -> None:
        rt = _MockRuntime()
        ctrl = json.dumps({"type": "resize", "cols": 120, "rows": 40}).encode()
        await handle_tunnel_message(rt, MagicMock(), b"\x00\x00" + ctrl)

    @pytest.mark.asyncio
    async def test_control_close(self) -> None:
        rt = _MockRuntime()
        ctrl = json.dumps({"type": "close", "channel": 1}).encode()
        await handle_tunnel_message(rt, MagicMock(), b"\x00\x00" + ctrl)

    @pytest.mark.asyncio
    async def test_control_invalid_json(self) -> None:
        rt = _MockRuntime()
        await handle_tunnel_message(rt, MagicMock(), b"\x00\x00not json")

    @pytest.mark.asyncio
    async def test_empty_payload_not_broadcast(self) -> None:
        rt = _MockRuntime()
        await handle_tunnel_message(rt, MagicMock(), b"\x01\x00")
        rt.broadcast_worker_frame.assert_not_called()


class TestTunnelApi:
    @pytest.mark.asyncio
    async def test_create_tunnel(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnels

        kv = MagicMock()
        kv.put = AsyncMock()
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.method = "POST"
        request.url = "https://example.com/api/tunnels"
        request.json = AsyncMock(return_value={"tunnel_type": "terminal", "display_name": "operator@host"})

        resp = await handle_tunnels(request, env)
        body = json.loads(resp.body)
        assert body["tunnel_id"].startswith("tunnel-")
        assert "worker_token" in body
        assert "/app/session/" in body["share_url"]
        assert "token=" in body["share_url"]
        assert "token=" in body["control_url"]
        kv.put.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_tunnel_method_not_allowed(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnels

        request = MagicMock()
        request.method = "GET"
        request.url = "https://example.com/api/tunnels"
        resp = await handle_tunnels(request, MagicMock())
        assert resp.status == 405

    @pytest.mark.asyncio
    async def test_resolve_share_context_valid_viewer_token(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import resolve_share_context

        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps({"share_token": "abc", "control_token": "def"}))
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.url = "https://example.com/app/session/tunnel-abc?token=abc"

        context = await resolve_share_context(request, env, "tunnel-abc")
        assert context == ("session", "viewer")

    @pytest.mark.asyncio
    async def test_resolve_share_context_valid_operator_token(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import resolve_share_context

        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps({"share_token": "abc", "control_token": "def"}))
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.url = "https://example.com/app/operator/tunnel-abc?token=def"

        context = await resolve_share_context(request, env, "tunnel-abc")
        assert context == ("operator", "operator")

    @pytest.mark.asyncio
    async def test_resolve_share_context_invalid_token(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import resolve_share_context

        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps({"share_token": "abc", "control_token": "def"}))
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.url = "https://example.com/app/session/tunnel-abc?token=wrong"

        context = await resolve_share_context(request, env, "tunnel-abc")
        assert context is None

    @pytest.mark.asyncio
    async def test_resolve_share_context_missing_tokens_does_not_fail_open(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import resolve_share_context

        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps({"session_id": "tunnel-abc"}))
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.url = "https://example.com/app/session/tunnel-abc"

        context = await resolve_share_context(request, env, "tunnel-abc")
        assert context is None


class TestTunnelRevokeTokens:
    @pytest.mark.asyncio
    async def test_revoke_existing_tunnel(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnel_revoke_tokens

        entry = {
            "session_id": "tunnel-abc",
            "worker_token": "w1",
            "share_token": "s1",
            "control_token": "c1",
        }
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(entry))
        kv.put = AsyncMock()
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()

        resp = await handle_tunnel_revoke_tokens(request, env, "tunnel-abc")
        body = json.loads(resp.body)
        assert resp.status == 200
        assert body["ok"] is True
        assert body["session_id"] == "tunnel-abc"
        kv.put.assert_called_once()
        stored = json.loads(kv.put.call_args[0][1])
        assert stored["worker_token"] is None
        assert stored["share_token"] is None
        assert stored["control_token"] is None

    @pytest.mark.asyncio
    async def test_revoke_nonexistent_tunnel(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnel_revoke_tokens

        kv = MagicMock()
        kv.get = AsyncMock(return_value=None)
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()

        resp = await handle_tunnel_revoke_tokens(request, env, "tunnel-nope")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_revoke_no_kv(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnel_revoke_tokens

        env = MagicMock(spec=[])
        request = MagicMock()

        resp = await handle_tunnel_revoke_tokens(request, env, "tunnel-x")
        assert resp.status == 500

    @pytest.mark.asyncio
    async def test_revoke_corrupt_entry(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnel_revoke_tokens

        kv = MagicMock()
        kv.get = AsyncMock(return_value="not-valid-json{{{")
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()

        resp = await handle_tunnel_revoke_tokens(request, env, "tunnel-bad")
        assert resp.status == 500


class TestTunnelRotateTokens:
    @pytest.mark.asyncio
    async def test_rotate_existing_tunnel(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnel_rotate_tokens

        entry = {
            "session_id": "tunnel-abc",
            "worker_token": "old_w",
            "share_token": "old_s",
            "control_token": "old_c",
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
        assert resp.status == 200
        assert body["tunnel_id"] == "tunnel-abc"
        assert body["worker_token"] != "old_w"
        assert "/app/session/tunnel-abc?token=" in body["share_url"]
        assert "/app/operator/tunnel-abc?token=" in body["control_url"]
        assert "expires_at" in body
        assert body["ws_endpoint"] == "/tunnel/tunnel-abc"
        kv.put.assert_called_once()
        stored = json.loads(kv.put.call_args[0][1])
        assert stored["worker_token"] == body["worker_token"]
        assert stored["share_token"] is not None
        assert stored["control_token"] is not None

    @pytest.mark.asyncio
    async def test_rotate_nonexistent_tunnel(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnel_rotate_tokens

        kv = MagicMock()
        kv.get = AsyncMock(return_value=None)
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.url = "https://example.com/api/tunnels/tunnel-nope/tokens/rotate"

        resp = await handle_tunnel_rotate_tokens(request, env, "tunnel-nope")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_rotate_no_kv(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnel_rotate_tokens

        env = MagicMock(spec=[])
        request = MagicMock()
        request.url = "https://example.com/api/tunnels/tunnel-x/tokens/rotate"

        resp = await handle_tunnel_rotate_tokens(request, env, "tunnel-x")
        assert resp.status == 500

    @pytest.mark.asyncio
    async def test_rotate_corrupt_entry(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnel_rotate_tokens

        kv = MagicMock()
        kv.get = AsyncMock(return_value="bad json!!!")
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.url = "https://example.com/api/tunnels/tunnel-bad/tokens/rotate"

        resp = await handle_tunnel_rotate_tokens(request, env, "tunnel-bad")
        assert resp.status == 500


class TestTunnelAuthz:
    """Ownership enforcement on tunnel management in CF worker.

    Mirrors the FastAPI server's tunnel access control: creating a tunnel
    records the caller as owner and marks the session private; rotate and
    revoke require owner-or-admin; service-token principals (admin) bypass.
    """

    @pytest.mark.asyncio
    async def test_create_tunnel_records_owner_and_private_visibility(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnels

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
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnels

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
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnel_revoke_tokens

        entry = {"session_id": "tunnel-abc", "owner": "alice", "share_token": "s", "control_token": "c"}
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
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnel_revoke_tokens

        entry = {"session_id": "tunnel-abc", "owner": "alice", "share_token": "s", "control_token": "c"}
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
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnel_revoke_tokens

        entry = {"session_id": "tunnel-abc", "owner": "alice", "share_token": "s", "control_token": "c"}
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
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnel_rotate_tokens

        entry = {
            "session_id": "tunnel-abc",
            "owner": "alice",
            "worker_token": "w",
            "share_token": "s",
            "control_token": "c",
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
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnel_rotate_tokens

        entry = {
            "session_id": "tunnel-abc",
            "owner": "alice",
            "worker_token": "w",
            "share_token": "s",
            "control_token": "c",
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
    """F3: share_url uses the right page kind (inspect for http, session otherwise)."""

    @pytest.mark.asyncio
    async def test_create_http_tunnel_share_url_points_to_inspect(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnels

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
        assert "/app/inspect/" in body["share_url"]
        assert "token=" in body["share_url"]

    @pytest.mark.asyncio
    async def test_create_terminal_tunnel_share_url_points_to_session(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnels

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
        assert "/app/session/" in body["share_url"]

    @pytest.mark.asyncio
    async def test_rotate_http_tunnel_share_url_points_to_inspect(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnel_rotate_tokens

        entry = {
            "session_id": "tunnel-abc",
            "tunnel_type": "http",
            "worker_token": "w",
            "share_token": "s",
            "control_token": "c",
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
        assert "/app/inspect/tunnel-abc?token=" in body["share_url"]


class TestTunnelRevocationBlocksAccess:
    """F2: Revoked tunnels must not grant viewer access via resolve_share_context."""

    @pytest.mark.asyncio
    async def test_revoked_tunnel_returns_none_with_no_token(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import resolve_share_context

        session = {"share_token": None, "control_token": None, "revoked": True}
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
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnel_revoke_tokens

        entry = {"session_id": "tunnel-abc", "share_token": "s", "control_token": "c"}
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
        from provide.terminal.cloudflare.api._tunnel_api import resolve_share_context

        session = {"share_token": "valid-tok", "control_token": "ctrl", "revoked": True}
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(session))
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.url = "https://example.com/app/session/tunnel-abc?token=valid-tok"

        result = await resolve_share_context(request, env, "tunnel-abc")
        assert result is None


class TestTunnelTokenTransportEnforcement:
    """F1: tunnel_token_transport config gates which transport(s) are accepted."""

    @pytest.mark.asyncio
    async def test_cookie_only_mode_rejects_query_token(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import resolve_share_context

        session = {"share_token": "tok", "control_token": "ctrl", "expires_at": time.time() + 3600}
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
        from provide.terminal.cloudflare.api._tunnel_api import resolve_share_context

        session = {"share_token": "tok", "control_token": "ctrl", "expires_at": time.time() + 3600}
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
    async def test_query_only_mode_rejects_cookie_token(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import resolve_share_context

        session = {"share_token": "tok", "control_token": "ctrl", "expires_at": time.time() + 3600}
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
        assert result is None


class TestTunnelIpBinding:
    """F1: tunnel_ip_binding config rejects tokens from mismatched IPs."""

    @pytest.mark.asyncio
    async def test_ip_match_allowed(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import resolve_share_context

        session = {
            "share_token": "tok",
            "control_token": "ctrl",
            "issued_ip": "1.2.3.4",
            "expires_at": time.time() + 3600,
        }
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(session))
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.url = "https://example.com/app/session/tunnel-abc?token=tok"
        request.headers = SimpleNamespace(get=lambda k, d=None: "1.2.3.4" if "Connecting-IP" in k else d)
        config = SimpleNamespace(tunnel_token_transport="both", tunnel_ip_binding=True)

        result = await resolve_share_context(request, env, "tunnel-abc", config)
        assert result == ("session", "viewer")

    @pytest.mark.asyncio
    async def test_ip_mismatch_rejected(self) -> None:
        from provide.terminal.cloudflare.api._tunnel_api import resolve_share_context

        session = {
            "share_token": "tok",
            "control_token": "ctrl",
            "issued_ip": "1.2.3.4",
            "expires_at": time.time() + 3600,
        }
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(session))
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.url = "https://example.com/app/session/tunnel-abc?token=tok"
        request.headers = SimpleNamespace(get=lambda k, d=None: "9.9.9.9" if "Connecting-IP" in k else d)
        config = SimpleNamespace(tunnel_token_transport="both", tunnel_ip_binding=True)

        result = await resolve_share_context(request, env, "tunnel-abc", config)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_issued_ip_skips_binding_check(self) -> None:
        """Empty issued_ip means binding was not configured at create time — allow."""
        from provide.terminal.cloudflare.api._tunnel_api import resolve_share_context

        session = {
            "share_token": "tok",
            "control_token": "ctrl",
            "issued_ip": "",
            "expires_at": time.time() + 3600,
        }
        kv = MagicMock()
        kv.get = AsyncMock(return_value=json.dumps(session))
        env = MagicMock()
        env.SESSION_REGISTRY = kv
        request = MagicMock()
        request.url = "https://example.com/app/session/tunnel-abc?token=tok"
        request.headers = SimpleNamespace(get=lambda k, d=None: "9.9.9.9" if "Connecting-IP" in k else d)
        config = SimpleNamespace(tunnel_token_transport="both", tunnel_ip_binding=True)

        result = await resolve_share_context(request, env, "tunnel-abc", config)
        assert result == ("session", "viewer")

    @pytest.mark.asyncio
    async def test_ip_binding_headers_get_exception_treats_as_no_client_ip(self) -> None:
        """Exception from headers.get in IP binding silently caught; empty client_ip != issued_ip → rejected."""
        from provide.terminal.cloudflare.api._tunnel_api import resolve_share_context

        session = {
            "share_token": "tok",
            "control_token": "ctrl",
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
