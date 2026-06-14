#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Dispatch integration tests for webhook and SSE HTTP routes."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from provide.uterm.cloudflare.api.http_routes import route_http
from provide.uterm.cloudflare.bridge.hijack import HijackCoordinator
from provide.uterm.cloudflare.state.store import SqliteStateStore


def _make_store() -> SqliteStateStore:
    conn = sqlite3.connect(":memory:")
    store = SqliteStateStore(conn.execute)
    store.migrate()
    return store


class _FullRuntime:
    worker_id = "w1"
    worker_ws = None
    hijack = HijackCoordinator()
    last_snapshot: dict | None = None
    last_analysis: str | None = None
    lifecycle_state = "stopped"
    input_mode = "hijack"
    browser_hijack_owner: dict = {}
    meta: dict = {"visibility": "public", "owner": None}

    def __init__(self, store: SqliteStateStore, *, request_body: bool = False) -> None:
        self.store = store
        self._request_body = request_body

    async def browser_role_for_request(self, request: object) -> str:
        return "admin"

    async def browser_subject_for_request(self, request: object) -> str | None:
        return None

    async def request_json(self, request: object) -> dict:
        if self._request_body:
            return json.loads(getattr(request, "_body", "{}"))
        return {}

    def persist_lease(self, session: object) -> None:
        pass

    def clear_lease(self) -> None:
        pass

    async def push_worker_control(self, action: str, *, owner: str, lease_s: int) -> bool:
        return False

    async def broadcast_hijack_state(self) -> None:
        pass

    async def push_worker_input(self, data: str) -> bool:
        return False

    async def send_ws(self, ws: object, frame: dict) -> None:
        pass

    def ws_key(self, ws: object) -> str:
        return str(id(ws))

    def _socket_browser_role(self, ws: object) -> str:
        return "admin"


@pytest.mark.asyncio
async def test_dispatch_sse_route() -> None:
    """SSE route dispatches correctly via route_http."""
    store = _make_store()
    store.append_event("w1", "snapshot", {"screen": "$ test"})
    runtime = _FullRuntime(store)
    req = SimpleNamespace(url="http://example.com/api/sessions/w1/events/stream", method="GET", headers={})

    resp = await route_http(runtime, req)  # type: ignore[arg-type]

    assert resp.status == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_dispatch_webhook_register_route() -> None:
    """Webhook POST route dispatches correctly via route_http."""
    store = _make_store()
    runtime = _FullRuntime(store, request_body=True)
    req = SimpleNamespace(
        url="http://example.com/api/sessions/w1/webhooks",
        method="POST",
        _body=json.dumps({"url": "https://example.com/hook"}),
    )

    resp = await route_http(runtime, req)  # type: ignore[arg-type]

    assert resp.status == 200
    data = json.loads(resp.body)
    assert "webhook_id" in data
