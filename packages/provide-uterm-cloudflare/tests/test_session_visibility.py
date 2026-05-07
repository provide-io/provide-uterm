#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for session visibility enforcement across HTTP routes and fleet listing."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from provide.terminal.cloudflare.api.http_routes import route_http
from provide.terminal.cloudflare.bridge.hijack import HijackCoordinator
from provide.terminal.cloudflare.config import CloudflareConfig


class _Req:
    def __init__(self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None):
        self.url = url
        self.method = method
        self.headers = headers or {}


class _Runtime:
    def __init__(self) -> None:
        self.worker_id = "w1"
        self.meta: dict = {
            "display_name": self.worker_id,
            "connector_type": "unknown",
            "created_at": 0.0,
            "tags": [],
            "visibility": "public",
            "owner": None,
        }
        self.worker_ws = object()
        self.hijack = HijackCoordinator()
        self.persisted: list[float] = []
        self.actions: list[tuple[str, str, int]] = []
        self._role = "admin"
        self._subject: str | None = None
        self.last_snapshot: dict | None = None
        self.browser_hijack_owner: dict[str, str] = {}
        self.lifecycle_state = "stopped"
        self.input_mode: str = "hijack"

    async def request_json(self, request: object) -> dict[str, object]:
        return json.loads(getattr(request, "_body", "{}"))

    async def browser_role_for_request(self, request: object) -> str:
        return self._role

    async def browser_subject_for_request(self, request: object) -> str | None:
        return self._subject

    def persist_lease(self, session: object) -> None:
        if session is not None:
            self.persisted.append(float(session.lease_expires_at))

    def clear_lease(self) -> None:
        return

    async def push_worker_control(self, action: str, *, owner: str, lease_s: int) -> bool:
        self.actions.append((action, owner, lease_s))
        return True

    async def broadcast_hijack_state(self) -> None:
        return

    async def push_worker_input(self, data: str) -> bool:
        return bool(data)

    @property
    def store(self) -> object:
        return SimpleNamespace(
            list_events_since=lambda *_args, **_kwargs: [],
            load_session=lambda *_args, **_kwargs: None,
            current_event_seq=lambda *_args, **_kwargs: 0,
            min_event_seq=lambda *_args, **_kwargs: 0,
            save_input_mode=lambda *_args, **_kwargs: None,
        )


# ---------------------------------------------------------------------------
# Per-session visibility enforcement (route_session GET)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_session_get_public_allows_viewer() -> None:
    """Public sessions are readable by any authenticated caller."""
    runtime = _Runtime()
    runtime.meta["visibility"] = "public"
    runtime._role = "viewer"
    req = _Req("https://example.invalid/api/sessions/w1")
    resp = await route_http(runtime, req)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_route_session_get_private_blocks_non_owner_viewer() -> None:
    """Private sessions return 403 for a viewer who is not the owner."""
    runtime = _Runtime()
    runtime.meta["visibility"] = "private"
    runtime.meta["owner"] = "alice"
    runtime._role = "viewer"
    runtime._subject = "bob"  # not the owner
    req = _Req("https://example.invalid/api/sessions/w1")
    resp = await route_http(runtime, req)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_route_session_get_private_allows_owner() -> None:
    """Private sessions are accessible to the subject who owns them."""
    runtime = _Runtime()
    runtime.meta["visibility"] = "private"
    runtime.meta["owner"] = "alice"
    runtime._role = "viewer"
    runtime._subject = "alice"  # is the owner
    req = _Req("https://example.invalid/api/sessions/w1")
    resp = await route_http(runtime, req)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_route_session_get_private_allows_admin() -> None:
    """Private sessions are accessible to admin callers regardless of ownership."""
    runtime = _Runtime()
    runtime.meta["visibility"] = "private"
    runtime.meta["owner"] = "alice"
    runtime._role = "admin"
    runtime._subject = "bob"  # not the owner, but admin
    req = _Req("https://example.invalid/api/sessions/w1")
    resp = await route_http(runtime, req)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_route_session_get_private_blocks_unauthenticated() -> None:
    """Private sessions return 403 when the caller has no subject (unauthenticated viewer)."""
    runtime = _Runtime()
    runtime.meta["visibility"] = "private"
    runtime.meta["owner"] = "alice"
    runtime._role = "viewer"
    runtime._subject = None  # no JWT subject
    req = _Req("https://example.invalid/api/sessions/w1")
    resp = await route_http(runtime, req)
    assert resp.status == 403


# ---------------------------------------------------------------------------
# Visibility enforcement on sub-routes (SSE, webhooks, recording)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_sse_private_session_blocks_non_owner() -> None:
    """SSE stream for a private session returns 403 for a non-owner viewer."""
    runtime = _Runtime()
    runtime.meta["visibility"] = "private"
    runtime.meta["owner"] = "alice"
    runtime._role = "viewer"
    runtime._subject = "bob"
    req = _Req("https://example.invalid/api/sessions/w1/events/stream")
    resp = await route_http(runtime, req)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_route_webhooks_private_session_blocks_non_owner() -> None:
    """Webhook route for a private session returns 403 for a non-owner viewer."""
    runtime = _Runtime()
    runtime.meta["visibility"] = "private"
    runtime.meta["owner"] = "alice"
    runtime._role = "viewer"
    runtime._subject = "bob"
    req = _Req("https://example.invalid/api/sessions/w1/webhooks", method="GET")
    resp = await route_http(runtime, req)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_route_recording_private_session_blocks_non_owner() -> None:
    """Recording route for a private session returns 403 for a non-owner viewer."""
    runtime = _Runtime()
    runtime.meta["visibility"] = "private"
    runtime.meta["owner"] = "alice"
    runtime._role = "viewer"
    runtime._subject = "bob"
    req = _Req("https://example.invalid/api/sessions/w1/recording")
    resp = await route_http(runtime, req)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_route_sse_operator_session_allows_operator() -> None:
    """Operator-visibility SSE route allows a caller with operator role."""
    runtime = _Runtime()
    runtime.meta["visibility"] = "operator"
    runtime.meta["owner"] = "alice"
    runtime._role = "operator"
    runtime._subject = "bob"  # not the owner but has operator role
    req = _Req("https://example.invalid/api/sessions/w1/events/stream")
    resp = await route_http(runtime, req)
    # Visibility check passes — route proceeds (200 or non-403)
    assert resp.status != 403


# ---------------------------------------------------------------------------
# Fleet GET: non-admin sees only accessible sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_sessions_get_filters_for_non_admin() -> None:
    """Non-admin JWT principal sees only sessions they own (when all are private)."""
    from provide.terminal.cloudflare.entry.handlers import _handle_sessions

    sessions = [
        {"session_id": "s1", "owner": "alice", "visibility": "private"},
        {"session_id": "s2", "owner": "bob", "visibility": "private"},
        {"session_id": "s3", "owner": "alice", "visibility": "private"},
    ]

    with patch(
        "provide.terminal.cloudflare.entry.auth._decode_jwt_principal",
        new=AsyncMock(return_value=SimpleNamespace(subject_id="alice", roles=("viewer",))),
    ):
        with patch("provide.terminal.cloudflare.entry.list_kv_sessions", new=AsyncMock(return_value=sessions)):
            resp = await _handle_sessions(
                SimpleNamespace(method="GET"), SimpleNamespace(SESSION_REGISTRY=AsyncMock()), CloudflareConfig()
            )
    body = json.loads(resp.body)
    assert all(s["owner"] == "alice" for s in body)
    assert len(body) == 2


@pytest.mark.asyncio
async def test_handle_sessions_get_admin_sees_all() -> None:
    """Admin JWT principal sees all sessions regardless of owner."""
    from provide.terminal.cloudflare.entry.handlers import _handle_sessions

    sessions = [
        {"session_id": "s1", "owner": "alice"},
        {"session_id": "s2", "owner": "bob"},
    ]

    with patch(
        "provide.terminal.cloudflare.entry.auth._decode_jwt_principal",
        new=AsyncMock(return_value=SimpleNamespace(subject_id="admin-user", roles=("admin",))),
    ):
        with patch("provide.terminal.cloudflare.entry.list_kv_sessions", new=AsyncMock(return_value=sessions)):
            resp = await _handle_sessions(
                SimpleNamespace(method="GET"), SimpleNamespace(SESSION_REGISTRY=AsyncMock()), CloudflareConfig()
            )
    body = json.loads(resp.body)
    assert len(body) == 2


@pytest.mark.asyncio
async def test_handle_sessions_get_viewer_sees_public_sessions() -> None:
    """A viewer (non-owner) can see public sessions from other owners."""
    from provide.terminal.cloudflare.entry.handlers import _handle_sessions

    sessions = [
        {"session_id": "s1", "owner": "alice", "visibility": "public"},
        {"session_id": "s2", "owner": "bob", "visibility": "public"},
        {"session_id": "s3", "owner": "alice", "visibility": "private"},
    ]

    with patch(
        "provide.terminal.cloudflare.entry.auth._decode_jwt_principal",
        new=AsyncMock(return_value=SimpleNamespace(subject_id="alice", roles=("viewer",))),
    ):
        with patch("provide.terminal.cloudflare.entry.list_kv_sessions", new=AsyncMock(return_value=sessions)):
            resp = await _handle_sessions(
                SimpleNamespace(method="GET"), SimpleNamespace(SESSION_REGISTRY=AsyncMock()), CloudflareConfig()
            )
    body = json.loads(resp.body)
    session_ids = {s["session_id"] for s in body}
    assert "s2" in session_ids  # non-owner public session visible


@pytest.mark.asyncio
async def test_handle_sessions_get_operator_sees_operator_sessions() -> None:
    """An operator (non-owner) can see operator-visibility sessions from other owners."""
    from provide.terminal.cloudflare.entry.handlers import _handle_sessions

    sessions = [
        {"session_id": "s1", "owner": "bob", "visibility": "operator"},
        {"session_id": "s2", "owner": "bob", "visibility": "private"},
    ]

    with patch(
        "provide.terminal.cloudflare.entry.auth._decode_jwt_principal",
        new=AsyncMock(return_value=SimpleNamespace(subject_id="alice", roles=("operator",))),
    ):
        with patch("provide.terminal.cloudflare.entry.list_kv_sessions", new=AsyncMock(return_value=sessions)):
            resp = await _handle_sessions(
                SimpleNamespace(method="GET"), SimpleNamespace(SESSION_REGISTRY=AsyncMock()), CloudflareConfig()
            )
    body = json.loads(resp.body)
    session_ids = {s["session_id"] for s in body}
    assert "s1" in session_ids
    assert "s2" not in session_ids
