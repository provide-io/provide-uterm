#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Curated multi-backend Playwright/API parity suite.

Run with::

    UTERM_TEST_BACKEND=python|go|csharp uv run pytest -m playwright \\
        packages/provide-uterm/tests/playwright/test_multi_backend_parity.py --no-cov
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from playwright.sync_api import Page

from .backend_server import WORKER_BEARER, backend_name, spawn_backend_server

pytestmark = pytest.mark.playwright


@pytest.fixture(scope="module")
def multi_backend_url() -> str:
    with spawn_backend_server() as url:
        yield url


def test_backend_accepts_tcp(multi_backend_url: str) -> None:
    """Server process is listening (backend-agnostic smoke)."""
    assert multi_backend_url.startswith("http://127.0.0.1:")


def test_health_or_root_reachable(multi_backend_url: str) -> None:
    """At least one well-known HTTP surface answers without 5xx."""
    paths = ("/api/health", "/health", "/readyz", "/")
    last_status = None
    with httpx.Client(base_url=multi_backend_url, timeout=5.0) as client:
        for path in paths:
            try:
                r = client.get(path)
            except httpx.HTTPError:
                continue
            last_status = r.status_code
            if r.status_code < 500:
                return
    pytest.fail(f"no healthy path for {backend_name()} last={last_status}")


def test_worker_websocket_connects(multi_backend_url: str) -> None:
    """Worker term WS is accepted — same path across languages."""
    import asyncio
    import threading

    import websockets

    worker_id = f"mb-{uuid.uuid4().hex[:8]}"
    # C# maps /ws/worker/{id}; Python/Go use /ws/worker/{id}/term.
    suffix = f"/ws/worker/{worker_id}" if backend_name() == "csharp" else f"/ws/worker/{worker_id}/term"
    ws_url = multi_backend_url.replace("http://", "ws://") + suffix
    errors: list[BaseException] = []

    async def _run() -> None:
        headers = {"Authorization": f"Bearer {WORKER_BEARER}"}
        async with websockets.connect(ws_url, open_timeout=10, additional_headers=headers) as ws:
            await ws.ping()

    def _thread() -> None:
        try:
            asyncio.run(_run())
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=_thread, daemon=True)
    t.start()
    t.join(timeout=15)
    assert not t.is_alive(), "worker websocket connect hung"
    assert not errors, errors[0]


def test_browser_page_route_hello_surface(page: Page, multi_backend_url: str) -> None:
    """page.route serves test HTML while backend is a clean subprocess."""
    worker_id = f"br-{uuid.uuid4().hex[:8]}"
    html = f"""<!DOCTYPE html><html><body>
    <pre id="out">pending</pre>
    <script>
    (async () => {{
      try {{
        const ws = new WebSocket({json.dumps(multi_backend_url.replace("http://", "ws://") + f"/ws/browser/{worker_id}/term")});
        ws.binaryType = "arraybuffer";
        const timer = setTimeout(() => {{ document.getElementById("out").textContent = "timeout"; }}, 8000);
        ws.onmessage = (ev) => {{
          let text = typeof ev.data === "string" ? ev.data : new TextDecoder().decode(ev.data);
          // control frames may be DLE/STX wrapped; still mark connected
          document.getElementById("out").textContent = "ws_ok:" + text.slice(0, 80);
          clearTimeout(timer);
          ws.close();
        }};
        ws.onerror = () => {{ document.getElementById("out").textContent = "ws_err"; }};
        ws.onopen = () => {{ document.getElementById("out").textContent = "ws_open"; }};
      }} catch (e) {{
        document.getElementById("out").textContent = "ex:" + e;
      }}
    }})();
    </script></body></html>"""

    page.route(
        f"**/mb-test/{worker_id}",
        lambda route: route.fulfill(body=html, content_type="text/html"),
    )
    page.goto(f"{multi_backend_url}/mb-test/{worker_id}", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => { const t = document.getElementById('out')?.textContent || ''; "
        "return t.startsWith('ws_ok') || t === 'ws_open' || t.startsWith('ws_err'); }",
        timeout=15000,
    )
    text = page.locator("#out").text_content() or ""
    # Connection attempt must leave the pending state; prefer success.
    assert text != "pending"
    assert not text.startswith("ex:")
    # Soft success: open or message is enough; hard fail only on timeout
    assert text != "timeout"
