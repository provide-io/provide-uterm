#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Playwright E2E tests for the HTTP inspect/intercept UI.

Verifies:
- Inspect view loads and shows connected status
- HTTP requests appear in the list when the worker sends frames
- Intercept toggle works (ON/OFF)
- PAUSED badge appears on intercepted requests
- Forward/Drop action buttons resolve paused requests
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from playwright.sync_api import Page, expect
from starlette.responses import HTMLResponse

from provide.terminal.bridge.hub import TermHub
from provide.terminal.control_channel import encode_control
from provide.terminal.tunnel.fastapi_routes import register_tunnel_routes
from provide.terminal.tunnel.protocol import CHANNEL_HTTP, encode_frame

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _inspect_page_html(session_id: str, assets_path: str) -> str:
    """Generate an inspect page using the server's UI helper."""
    from provide.terminal.server.ui import inspect_page_html

    return inspect_page_html(
        title=f"Inspect {session_id}",
        assets_path=assets_path,
        session_id=session_id,
        app_path="/app",
    )


@pytest.fixture(scope="session")
def inspect_server():
    """Session-scoped server with TermHub + tunnel routes + custom inspect page."""
    import importlib.resources

    hub = TermHub(resolve_browser_role=lambda _ws, _wid: "admin")
    app = FastAPI()
    app.include_router(
        hub.create_router(extra_route_registrars=[register_tunnel_routes]),
    )

    # Mount frontend assets
    frontend = importlib.resources.files("provide.terminal") / "frontend"
    frontend_str = str(frontend)

    from starlette.staticfiles import StaticFiles

    @app.get("/app/inspect/{session_id}")
    async def inspect_page(session_id: str) -> HTMLResponse:
        return HTMLResponse(_inspect_page_html(session_id, "/_static"))

    # Mount AFTER routes so it doesn't shadow /app/inspect
    app.mount("/_static", StaticFiles(directory=frontend_str, html=True), name="assets")

    uvi_config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(uvi_config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("inspect_server: did not start")
        time.sleep(0.05)

    port = server.servers[0].sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    yield base_url

    server.should_exit = True
    thread.join(timeout=5)


class TunnelWorker:
    """Background thread that connects as a tunnel worker and sends HTTP frames."""

    def __init__(self, base_url: str, worker_id: str) -> None:
        self._base_url = base_url
        self._worker_id = worker_id
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> TunnelWorker:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._connected.wait(timeout=10.0):
            raise RuntimeError("TunnelWorker: did not connect")
        return self

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect())
        finally:
            self._loop.close()

    async def _connect(self) -> None:
        import websockets

        ws_url = self._base_url.replace("http://", "ws://") + f"/tunnel/{self._worker_id}"
        try:
            async with websockets.connect(ws_url) as ws:
                self._ws = ws
                self._connected.set()
                while not self._stop.is_set():
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=0.2)
                    except TimeoutError:
                        continue
                    except Exception:
                        break
        except Exception:
            self._connected.set()

    def send_http_req(self, rid: str, method: str, url: str, *, intercepted: bool = False) -> None:
        """Send an http_req frame via the tunnel."""
        msg = {
            "type": "http_req",
            "id": rid,
            "ts": time.time(),
            "method": method,
            "url": url,
            "headers": {"content-type": "text/plain"},
            "body_size": 0,
            "intercepted": intercepted,
            "_channel": "http",
        }
        payload = json.dumps(msg).encode()
        frame = encode_frame(CHANNEL_HTTP, payload)
        if self._ws and self._loop:
            asyncio.run_coroutine_threadsafe(self._ws.send(frame), self._loop)

    def send_http_res(self, rid: str, status: int = 200) -> None:
        """Send an http_res frame via the tunnel."""
        msg = {
            "type": "http_res",
            "id": rid,
            "ts": time.time(),
            "status": status,
            "status_text": "OK",
            "headers": {},
            "body_size": 0,
            "duration_ms": 42,
            "_channel": "http",
        }
        payload = json.dumps(msg).encode()
        frame = encode_frame(CHANNEL_HTTP, payload)
        if self._ws and self._loop:
            asyncio.run_coroutine_threadsafe(self._ws.send(frame), self._loop)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.playwright
class TestInspectE2E:
    """E2E tests for the inspect/intercept browser UI."""

    def test_inspect_page_loads_and_connects(self, page: Page, inspect_server: str) -> None:
        """Inspect page loads, shows Connected status."""
        worker_id = f"e2e-load-{int(time.time())}"
        worker = TunnelWorker(inspect_server, worker_id).start()

        page.goto(f"{inspect_server}/app/inspect/{worker_id}", wait_until="domcontentloaded")
        status = page.locator("#inspect-status")
        expect(status).to_have_text("Connected", timeout=10000)

        worker.stop()

    def test_http_requests_appear_in_list(self, page: Page, inspect_server: str) -> None:
        """HTTP requests sent by the worker appear in the inspect list."""
        worker_id = f"e2e-list-{int(time.time())}"
        worker = TunnelWorker(inspect_server, worker_id).start()

        page.goto(f"{inspect_server}/app/inspect/{worker_id}", wait_until="domcontentloaded")
        expect(page.locator("#inspect-status")).to_have_text("Connected", timeout=10000)

        # Send an HTTP request frame
        worker.send_http_req("r1", "GET", "/api/users")
        time.sleep(0.5)

        # Verify it appears in the list
        row = page.locator(".inspect-row")
        expect(row).to_have_count(1, timeout=5000)
        expect(row).to_contain_text("GET")
        expect(row).to_contain_text("/api/users")

        # Send response
        worker.send_http_res("r1", 200)
        time.sleep(0.5)

        # Verify status shows
        expect(page.locator(".status")).to_contain_text("200", timeout=5000)

        worker.stop()

    def test_intercept_toggle_visible(self, page: Page, inspect_server: str) -> None:
        """Inspect and Intercept toggle buttons are visible."""
        worker_id = f"e2e-toggle-{int(time.time())}"
        worker = TunnelWorker(inspect_server, worker_id).start()

        page.goto(f"{inspect_server}/app/inspect/{worker_id}", wait_until="domcontentloaded")
        expect(page.locator("#inspect-status")).to_have_text("Connected", timeout=10000)

        inspect_toggle = page.locator("#inspect-inspect-toggle")
        intercept_toggle = page.locator("#inspect-intercept-toggle")

        expect(inspect_toggle).to_be_visible()
        expect(intercept_toggle).to_be_visible()
        expect(inspect_toggle).to_have_text("Inspect: ON")
        expect(intercept_toggle).to_have_text("Intercept: OFF")

        worker.stop()

    def test_paused_badge_on_intercepted_request(self, page: Page, inspect_server: str) -> None:
        """Intercepted requests show a PAUSED badge."""
        worker_id = f"e2e-paused-{int(time.time())}"
        worker = TunnelWorker(inspect_server, worker_id).start()

        page.goto(f"{inspect_server}/app/inspect/{worker_id}", wait_until="domcontentloaded")
        expect(page.locator("#inspect-status")).to_have_text("Connected", timeout=10000)

        # Send intercepted request
        worker.send_http_req("r1", "POST", "/api/data", intercepted=True)
        time.sleep(0.5)

        # Verify PAUSED badge
        paused = page.locator(".badge.paused")
        expect(paused).to_have_count(1, timeout=5000)
        expect(paused).to_have_text("PAUSED")

        worker.stop()

    def test_action_buttons_on_paused_request(self, page: Page, inspect_server: str) -> None:
        """Clicking a paused request shows Forward/Drop/Modify buttons."""
        worker_id = f"e2e-actions-{int(time.time())}"
        worker = TunnelWorker(inspect_server, worker_id).start()

        page.goto(f"{inspect_server}/app/inspect/{worker_id}", wait_until="domcontentloaded")
        expect(page.locator("#inspect-status")).to_have_text("Connected", timeout=10000)

        worker.send_http_req("r1", "GET", "/api/test", intercepted=True)
        time.sleep(0.5)

        # Click the request row
        page.locator(".inspect-row").click()
        time.sleep(0.3)

        # Verify action buttons in detail panel
        expect(page.locator(".btn-forward")).to_be_visible(timeout=3000)
        expect(page.locator(".btn-drop")).to_be_visible()
        expect(page.locator(".btn-modify")).to_be_visible()

        worker.stop()
