#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""H4: webhook install requires mutate capability, not mere public-session read."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from provide.uterm.cloudflare.api.http_routes._dispatch import route_http
from provide.uterm.cloudflare.do._webhooks import route_webhooks
from provide.uterm.cloudflare.state.store import SqliteStateStore


class _Resp:
    def __init__(self, status: int, body: bytes = b"{}") -> None:
        self.status = status
        self.body = body


@pytest.mark.asyncio
async def test_public_session_viewer_cannot_register_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    """Visibility=public allows read but webhook POST must still require mutate."""

    async def _role(_req: object) -> str:
        return "viewer"

    async def _subject(_req: object) -> str:
        return "viewer-1"

    runtime = SimpleNamespace(
        worker_id="w1",
        meta={"visibility": "public", "owner": "owner-1"},
        browser_role_for_request=_role,
        browser_subject_for_request=_subject,
        request_json=AsyncMock(return_value={"url": "https://example.com/hook"}),
    )

    # route_http uses json_response from cf_types — ensure visibility allows through
    # then mutate gate returns 403.
    req = SimpleNamespace(
        method="POST",
        url="https://edge.example/api/sessions/w1/webhooks",
        headers=SimpleNamespace(get=lambda *_a, **_k: None),
    )
    # Avoid CSRF block
    monkeypatch.setattr(
        "provide.uterm.cloudflare.api.http_routes._dispatch._is_cross_site",
        lambda *_a, **_k: False,
    )
    resp = await route_http(runtime, req)
    status = getattr(resp, "status", None) or getattr(resp, "status_code", None)
    assert status == 403


@pytest.mark.asyncio
async def test_route_webhooks_register_rejects_http_and_metadata() -> None:
    """URL filter rejects http/metadata/loopback at registration."""
    conn = sqlite3.connect(":memory:")
    store = SqliteStateStore(conn.execute)
    store.migrate()

    class _Runtime:
        def __init__(self) -> None:
            self.store = store
            self.worker_id = "w1"
            self.env = None

        async def request_json(self, _req: object) -> dict:
            return self._body  # type: ignore[attr-defined]

    runtime = _Runtime()
    for bad in ("http://example.com/hook", "https://169.254.169.254/hook", "https://127.0.0.1/hook"):
        runtime._body = {"url": bad}
        req = SimpleNamespace(method="POST", url="https://x/api/sessions/w1/webhooks")
        resp = await route_webhooks(runtime, req, "/api/sessions/w1/webhooks", str(req.url), "POST", "w1")
        assert getattr(resp, "status", None) == 422, bad
