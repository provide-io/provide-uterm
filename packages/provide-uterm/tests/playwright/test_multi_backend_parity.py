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

import asyncio
import json
import threading
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page

from provide.uterm.control_channel import ControlChunk, ControlFrameDecoder

from .backend_server import WORKER_BEARER, BackendServer, backend_name, spawn_backend_server

pytestmark = pytest.mark.playwright

_VECTORS = json.loads(
    (Path(__file__).resolve().parents[4] / "spec" / "behavior_vectors.json").read_text(encoding="utf-8")
)
_HELLO_DEFAULTS = _VECTORS["hello_defaults"]


def _hello_defaults_for_backend() -> dict[str, bool]:
    b = backend_name()
    if b == "go":
        return _HELLO_DEFAULTS["go"]
    if b == "csharp":
        return _HELLO_DEFAULTS["csharp"]
    return _HELLO_DEFAULTS["python_fastapi"]


def _browser_ws_path(worker_id: str) -> str:
    if backend_name() == "csharp":
        return f"/ws/browser/{worker_id}"
    return f"/ws/browser/{worker_id}/term"


def _worker_ws_path(worker_id: str) -> str:
    if backend_name() == "csharp":
        return f"/ws/worker/{worker_id}"
    return f"/ws/worker/{worker_id}/term"


@pytest.fixture(scope="module")
def multi_backend() -> BackendServer:
    with spawn_backend_server() as srv:
        yield srv


def test_backend_accepts_tcp(multi_backend: BackendServer) -> None:
    assert multi_backend.base_url.startswith("http://127.0.0.1:")
    assert multi_backend.jwt


def test_health_or_root_reachable(multi_backend: BackendServer) -> None:
    paths = ("/api/health", "/health", "/readyz", "/healthz", "/")
    last_status = None
    with httpx.Client(base_url=multi_backend.base_url, timeout=5.0) as client:
        for path in paths:
            try:
                r = client.get(path)
            except httpx.HTTPError:
                continue
            last_status = r.status_code
            if r.status_code < 500:
                return
    pytest.fail(f"no healthy path for {backend_name()} last={last_status}")


def test_worker_websocket_connects(multi_backend: BackendServer) -> None:
    import websockets

    worker_id = f"mb-{uuid.uuid4().hex[:8]}"
    ws_url = multi_backend.base_url.replace("http://", "ws://") + _worker_ws_path(worker_id)
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


def test_browser_hello_capability_wire_parity(multi_backend: BackendServer) -> None:
    """Dial browser WS with JWT; assert hello capability defaults from contract."""
    import websockets

    # Use configured public session "demo" so Go/C# registry CanReadSession passes.
    worker_id = "demo"
    ws_url = multi_backend.base_url.replace("http://", "ws://") + _browser_ws_path(worker_id)
    expected = _hello_defaults_for_backend()
    result: dict[str, object] = {}
    errors: list[BaseException] = []

    async def _run() -> None:
        headers = {"Authorization": f"Bearer {multi_backend.jwt}"}
        async with websockets.connect(ws_url, open_timeout=10, additional_headers=headers) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=8)
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            dec = ControlFrameDecoder()
            for chunk in dec.feed(text):
                if isinstance(chunk, ControlChunk) and chunk.control.get("type") == "hello":
                    result["hello"] = chunk.control
                    return
            result["raw"] = text[:200]

    def _thread() -> None:
        try:
            asyncio.run(_run())
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=_thread, daemon=True)
    t.start()
    t.join(timeout=20)
    assert not t.is_alive(), "browser hello dial hung"
    assert not errors, errors[0]
    hello = result.get("hello")
    assert isinstance(hello, dict), f"no hello frame; got {result!r}"
    assert "mcp_supported" in hello, f"mcp_supported missing: {hello}"
    assert "vnc_supported" in hello, f"vnc_supported missing: {hello}"
    assert bool(hello["mcp_supported"]) is expected["mcp_supported"], hello
    assert bool(hello["vnc_supported"]) is expected["vnc_supported"], hello


def test_browser_page_route_receives_hello(page: Page, multi_backend: BackendServer) -> None:
    """Browser page.route path: JWT via query/header is not available in raw WS from page.

    Proves page.route isolation still works by serving HTML that fetches /api/health
    (no soft-fail on WS). Hello capability wire is covered by
    test_browser_hello_capability_wire_parity above.
    """
    worker_id = f"br-{uuid.uuid4().hex[:8]}"
    health_url = multi_backend.base_url + "/api/health"
    html = f"""<!DOCTYPE html><html><body>
    <pre id="out">pending</pre>
    <script>
    (async () => {{
      try {{
        const r = await fetch({json.dumps(health_url)});
        document.getElementById("out").textContent = "http_ok:" + r.status;
      }} catch (e) {{
        document.getElementById("out").textContent = "ex:" + e;
      }}
    }})();
    </script></body></html>"""

    page.route(
        f"**/mb-test/{worker_id}",
        lambda route: route.fulfill(body=html, content_type="text/html"),
    )
    page.goto(f"{multi_backend.base_url}/mb-test/{worker_id}", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => { const t = document.getElementById('out')?.textContent || ''; "
        "return t.startsWith('http_ok:') || t.startsWith('ex:'); }",
        timeout=10000,
    )
    text = page.locator("#out").text_content() or ""
    assert text.startswith("http_ok:"), f"page.route backend unreachable: {text!r}"
    # health may be 200 or 401 depending on backend auth — not 5xx / network fail
    status = int(text.split(":", 1)[1])
    assert status < 500, text
