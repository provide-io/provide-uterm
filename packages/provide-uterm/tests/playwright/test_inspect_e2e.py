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

Dual-mode:
- default python → in-process TermHub + tunnel routes (fast)
- UTERM_MULTI_BACKEND / go / csharp → real language server /tunnel + page.route UI
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from playwright.sync_api import Page, Route, expect
from starlette.responses import HTMLResponse

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.tunnel.fastapi_routes import register_tunnel_routes
from provide.uterm.tunnel.protocol import CHANNEL_HTTP, encode_frame

from .backend_server import FRONTEND_DIR, WORKER_BEARER, stop_uvicorn_thread
from .ui_routes import multi_backend_env

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _inspect_page_html(session_id: str, assets_path: str) -> str:
    """Generate an inspect page using the server's UI helper."""
    from provide.uterm.server.ui import inspect_page_html

    return inspect_page_html(
        title=f"Inspect {session_id}",
        assets_path=assets_path,
        session_id=session_id,
        app_path="/app",
    )


@pytest.fixture(scope="session")
def inspect_server():
    """Session-scoped inspect surface (dual-mode).

    * default python in-process TermHub + /tunnel + /app/inspect
    * multi-backend / go / csharp → subprocess server; UI via page.route
    """
    import importlib.resources

    if multi_backend_env():
        backend = os.environ.get("UTERM_TEST_BACKEND", "python").strip().lower() or "python"
        os.environ["UTERM_TEST_BACKEND"] = backend if backend in ("python", "go", "csharp") else "python"
        from .backend_server import spawn_backend_server

        os.environ["UTERM_TEST_WORKER_BEARER"] = WORKER_BEARER
        with spawn_backend_server() as srv:
            yield srv.base_url, srv.jwt
        return

    hub = TermHub(resolve_browser_role=lambda _ws, _wid: "admin")
    app = FastAPI()
    app.include_router(
        hub.create_router(extra_route_registrars=[register_tunnel_routes]),
    )

    frontend = importlib.resources.files("provide.uterm.server") / "frontend"
    frontend_str = str(frontend)

    from starlette.staticfiles import StaticFiles

    @app.get("/app/inspect/{session_id}")
    async def inspect_page(session_id: str) -> HTMLResponse:
        return HTMLResponse(_inspect_page_html(session_id, "/_static"))

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
    yield base_url, ""

    stop_uvicorn_thread(server, thread)


def _install_inspect_routes(page: Page) -> None:
    """Serve inspect HTML + /ui assets when multi-backend (subprocess has no full SPA)."""
    if not multi_backend_env():
        return
    if getattr(page, "_uterm_inspect_routes", False):
        return
    fe = FRONTEND_DIR

    def on_inspect(route: Route) -> None:
        url = route.request.url
        parts = url.rstrip("/").split("/app/inspect/")
        session_id = parts[-1].split("?")[0] if len(parts) > 1 else "unknown"
        # Assets under /ui for multi-backend page.route (matches backend.toml assets_path).
        html = _inspect_page_html(session_id, "/ui")
        route.fulfill(status=200, content_type="text/html", body=html)

    def on_ui(route: Route) -> None:
        import mimetypes
        from pathlib import Path

        url = route.request.url
        rel = url.split("/ui/", 1)[-1].split("?")[0]
        path = (fe / rel).resolve()
        try:
            path.relative_to(Path(fe).resolve())
        except ValueError:
            route.fulfill(status=403, body="forbidden")
            return
        if not path.is_file():
            # Try hashed assets: resolve vanilla-manifest main entry.
            if rel in ("assets/main.js", "assets/main.css"):
                manifest = fe / "vanilla-manifest.json"
                if manifest.is_file():
                    try:
                        data = json.loads(manifest.read_text(encoding="utf-8"))
                        key = "main" if rel.endswith(".js") else "main_css"
                        alt = data.get(key) or data.get("main.js") or data.get("main")
                        if isinstance(alt, str):
                            path = (fe / alt.lstrip("/")).resolve()
                    except Exception:
                        pass
            if not path.is_file():
                # Fallback: first main-*.js / main-*.css under assets/
                assets = fe / "assets"
                if assets.is_dir():
                    prefix = "main-" if "main" in rel else ""
                    suffix = ".js" if rel.endswith(".js") else ".css"
                    for p in sorted(assets.glob(f"{prefix}*{suffix}")):
                        path = p
                        break
            if not path.is_file():
                route.fulfill(status=404, body=f"missing {rel}")
                return
        mime, _ = mimetypes.guess_type(str(path))
        route.fulfill(status=200, content_type=mime or "application/octet-stream", body=path.read_bytes())

    page.route("**/app/inspect/**", on_inspect)
    page.route("**/ui/**", on_ui)
    page._uterm_inspect_routes = True  # type: ignore[attr-defined]


def _goto_inspect(page: Page, base_url: str, worker_id: str, jwt: str) -> None:
    _install_inspect_routes(page)
    if jwt:
        page.set_extra_http_headers({"Authorization": f"Bearer {jwt}"})
    page.goto(f"{base_url}/app/inspect/{worker_id}", wait_until="domcontentloaded")


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
        self._error: str | None = None

    def start(self) -> TunnelWorker:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._connected.wait(timeout=15.0):
            raise RuntimeError(f"TunnelWorker: did not connect ({self._error or 'timeout'})")
        if self._ws is None:
            raise RuntimeError(f"TunnelWorker: connect failed ({self._error or 'unknown'})")
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
        headers: dict[str, str] = {}
        bearer = os.environ.get("UTERM_TEST_WORKER_BEARER", "").strip() or (
            WORKER_BEARER if multi_backend_env() else ""
        )
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        try:
            async with websockets.connect(
                ws_url,
                additional_headers=headers or None,
                open_timeout=10,
            ) as ws:
                self._ws = ws
                self._connected.set()
                while not self._stop.is_set():
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=0.2)
                    except TimeoutError:
                        continue
                    except Exception:
                        break
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
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

    def test_inspect_page_loads_and_connects(self, page: Page, inspect_server: tuple[str, str]) -> None:
        """Inspect page loads, shows Connected status."""
        base_url, jwt = inspect_server
        worker_id = f"e2e-load-{int(time.time())}"
        worker = TunnelWorker(base_url, worker_id).start()

        _goto_inspect(page, base_url, worker_id, jwt)
        expect(page.get_by_text("Connected")).to_be_visible(timeout=15000)

        worker.stop()

    def test_http_requests_appear_in_list(self, page: Page, inspect_server: tuple[str, str]) -> None:
        """HTTP requests sent by the worker appear in the inspect list."""
        base_url, jwt = inspect_server
        worker_id = f"e2e-list-{int(time.time())}"
        worker = TunnelWorker(base_url, worker_id).start()

        _goto_inspect(page, base_url, worker_id, jwt)
        expect(page.get_by_text("Connected")).to_be_visible(timeout=15000)

        worker.send_http_req("r1", "GET", "/api/users")
        time.sleep(0.5)

        expect(page.locator("span", has_text="GET").first).to_be_visible(timeout=8000)
        expect(page.get_by_text("/api/users")).to_be_visible(timeout=5000)

        worker.send_http_res("r1", 200)
        time.sleep(0.5)

        expect(page.get_by_text("200")).to_be_visible(timeout=5000)

        worker.stop()

    def test_intercept_toggle_visible(self, page: Page, inspect_server: tuple[str, str]) -> None:
        """Inspect and Intercept toggle buttons are visible."""
        base_url, jwt = inspect_server
        worker_id = f"e2e-toggle-{int(time.time())}"
        worker = TunnelWorker(base_url, worker_id).start()

        _goto_inspect(page, base_url, worker_id, jwt)
        expect(page.get_by_text("Connected")).to_be_visible(timeout=15000)

        inspect_toggle = page.get_by_role("button", name="Inspect: ON")
        intercept_toggle = page.get_by_role("button", name="Intercept: OFF")

        expect(inspect_toggle).to_be_visible()
        expect(intercept_toggle).to_be_visible()

        worker.stop()

    def test_paused_badge_on_intercepted_request(self, page: Page, inspect_server: tuple[str, str]) -> None:
        """Intercepted requests show a PAUSED badge."""
        base_url, jwt = inspect_server
        worker_id = f"e2e-paused-{int(time.time())}"
        worker = TunnelWorker(base_url, worker_id).start()

        _goto_inspect(page, base_url, worker_id, jwt)
        expect(page.get_by_text("Connected")).to_be_visible(timeout=15000)

        worker.send_http_req("r1", "POST", "/api/data", intercepted=True)
        time.sleep(0.5)

        expect(page.get_by_text("PAUSED")).to_be_visible(timeout=8000)

        worker.stop()

    def test_action_buttons_on_paused_request(self, page: Page, inspect_server: tuple[str, str]) -> None:
        """Clicking a paused request shows Forward/Drop/Modify buttons."""
        base_url, jwt = inspect_server
        worker_id = f"e2e-actions-{int(time.time())}"
        worker = TunnelWorker(base_url, worker_id).start()

        _goto_inspect(page, base_url, worker_id, jwt)
        expect(page.get_by_text("Connected")).to_be_visible(timeout=15000)

        worker.send_http_req("r1", "GET", "/api/test", intercepted=True)
        time.sleep(0.5)

        page.get_by_text("/api/test").click()
        time.sleep(0.3)

        expect(page.get_by_role("button", name="Forward", exact=True)).to_be_visible(timeout=5000)
        expect(page.get_by_role("button", name="Drop")).to_be_visible()
        expect(page.get_by_role("button", name="Modify & Forward")).to_be_visible()

        worker.stop()
