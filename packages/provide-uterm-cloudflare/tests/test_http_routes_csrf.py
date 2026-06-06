#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""CSRF cross-site guard tests for api/http_routes (_is_cross_site + route_http).

The guard runs in route_http before any route handler, so these tests target
``/api/health`` (handled before routing, never touching the runtime) with a
placeholder runtime — every _is_cross_site branch and the state-changing gate
are exercised without needing a full session runtime.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from provide.uterm.cloudflare.api.http_routes import route_http

# The CSRF guard consults only the request (method + headers + url), never the
# runtime, and /api/health is answered before routing — so a bare stub suffices.
_RT = SimpleNamespace()


def _req_h(url: str, *, method: str = "POST", headers: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(url=url, method=method, headers=headers or {}, _body="{}")


def _body(resp: object) -> dict:
    return json.loads(getattr(resp, "body", "{}") or "{}")


async def test_cross_site_post_blocked_via_sec_fetch_site() -> None:
    """Sec-Fetch-Site: cross-site on a state-changing POST → 403 cross_site_blocked."""
    resp = await route_http(_RT, _req_h("https://x/api/health", headers={"Sec-Fetch-Site": "cross-site"}))
    assert resp.status == 403
    assert _body(resp)["error"] == "cross_site_blocked"


async def test_same_origin_post_allowed_via_sec_fetch_site() -> None:
    """Sec-Fetch-Site: same-origin proceeds (not CSRF-blocked)."""
    resp = await route_http(_RT, _req_h("https://x/api/health", headers={"Sec-Fetch-Site": "same-origin"}))
    assert _body(resp)["ok"] is True


async def test_cross_site_post_blocked_via_origin_fallback() -> None:
    """No Sec-Fetch-Site, Origin host != request host → 403."""
    resp = await route_http(_RT, _req_h("https://x/api/health", headers={"Origin": "https://evil.example"}))
    assert resp.status == 403
    assert _body(resp)["error"] == "cross_site_blocked"


async def test_origin_null_blocked() -> None:
    """An opaque Origin (null) on a state-changing POST → 403."""
    resp = await route_http(_RT, _req_h("https://x/api/health", headers={"Origin": "null"}))
    assert resp.status == 403


async def test_same_origin_via_origin_header_allowed() -> None:
    """Origin host == request host proceeds."""
    resp = await route_http(_RT, _req_h("https://x/api/health", headers={"Origin": "https://x"}))
    assert _body(resp)["ok"] is True


async def test_post_with_headers_but_no_csrf_signals_allowed() -> None:
    """Headers present but no Sec-Fetch-Site/Origin (non-browser client) → proceeds."""
    resp = await route_http(_RT, _req_h("https://x/api/health", headers={}))
    assert _body(resp)["ok"] is True


async def test_get_cross_site_not_blocked() -> None:
    """A GET is never CSRF-blocked, even with Sec-Fetch-Site: cross-site."""
    resp = await route_http(_RT, _req_h("https://x/api/health", method="GET", headers={"Sec-Fetch-Site": "cross-site"}))
    assert _body(resp)["ok"] is True
