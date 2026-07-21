#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""H4: webhook install requires mutate capability, not mere public-session read."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from provide.uterm.cloudflare.api.http_routes._dispatch import route_http


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
