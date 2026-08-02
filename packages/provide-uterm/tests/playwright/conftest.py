#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Playwright test configuration: markers, fixtures, and reusable helpers."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from playwright.sync_api import Page

from provide.uterm.control_channel import encode_control_frame
from provide.uterm.server.bridge.hub import TermHub

from .backend_server import stop_uvicorn_thread


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict) -> dict:
    """Allow loopback WebSocket from Chromium under multi-backend e2e.

    Chromium Local Network Access checks can block ``ws://127.0.0.1`` from
    pages also on loopback when running under Playwright, which breaks
    UTERM_TEST_BACKEND=go|csharp hijack suites.
    """
    args = list(browser_type_launch_args.get("args") or [])
    args.append("--disable-features=LocalNetworkAccessChecks,BlockInsecurePrivateNetworkRequests")
    args.append("--allow-insecure-localhost")
    return {**browser_type_launch_args, "args": args}


def pytest_collection_modifyitems(items: list) -> None:
    """Mark all PT playwright tests and move ALL playwright-marked tests to the end.

    This ensures playwright tests (which start real browsers and async servers)
    always run after all other tests, regardless of how pytest discovers them.
    Without this, PT playwright tests can end up interleaved with tests from
    other packages (e.g. provide-uterm-cloudflare), corrupting asyncio state.
    """
    marker = pytest.mark.playwright
    # First pass: add marker to PT playwright tests by path.
    for item in items:
        if "tests/playwright/" in str(item.fspath):
            item.add_marker(marker)
    # Second pass: move ALL playwright-marked items (PT + CF + any other package)
    # to the end so they never corrupt asyncio state for other tests.
    playwright_items = []
    other_items = []
    for item in items:
        if item.get_closest_marker("playwright") is not None:
            playwright_items.append(item)
        else:
            other_items.append(item)
    items[:] = other_items + playwright_items


# ---------------------------------------------------------------------------
# Auto-auth: attach admin header-mode credentials to same-host requests only.
# The reference-server fixtures run in `header` auth mode for tests; without
# these headers every page navigation is 401. Adding them globally via
# ``extra_http_headers`` triggers CORS preflight for cross-origin assets
# (CDN-hosted xterm.js etc.) and breaks unrelated tests, so we use route
# interception to add them only for 127.0.0.1 / localhost.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _attach_uterm_auth_headers(page: Page) -> None:
    # The session/terminal UI now lives inside the <uterm-session>/<uterm-terminal>
    # web components' (open) shadow roots. Raw `document.querySelector` used in
    # wait_for_function/evaluate bodies does not pierce shadow DOM, so expose a
    # deep query helper that walks light DOM + every open shadowRoot. It is a
    # superset of document.querySelector (still finds light-DOM nodes), so call
    # sites can use it uniformly.
    page.add_init_script(
        """
        window.__deepQuery = (sel) => {
          const search = (root) => {
            const direct = root.querySelector(sel);
            if (direct) return direct;
            for (const el of root.querySelectorAll('*')) {
              if (el.shadowRoot) {
                const found = search(el.shadowRoot);
                if (found) return found;
              }
            }
            return null;
          };
          return search(document);
        };
        window.__deepQueryAll = (sel) => {
          const out = [];
          const search = (root) => {
            for (const el of root.querySelectorAll(sel)) out.push(el);
            for (const el of root.querySelectorAll('*')) {
              if (el.shadowRoot) search(el.shadowRoot);
            }
          };
          search(document);
          return out;
        };
        """
    )

    def _inject(route: Any) -> None:
        req = route.request
        url = req.url
        if "127.0.0.1" in url or "://localhost" in url:
            headers = {
                **req.headers,
                "X-Uterm-Principal": "admin",
                "X-Uterm-Role": "admin",
            }
            route.continue_(headers=headers)
        else:
            route.continue_()

    page.route("**/*", _inject)
    # WebSocket upgrades bypass page.route(), so we also drop principal/role
    # cookies. The header-mode auth resolver reads uterm_principal/uterm_role
    # cookies as a fallback when the headers are missing.
    page.context.add_cookies(
        [
            {
                "name": "uterm_principal",
                "value": "admin",
                "url": "http://127.0.0.1",
            },
            {
                "name": "uterm_role",
                "value": "admin",
                "url": "http://127.0.0.1",
            },
            {
                "name": "uterm_principal",
                "value": "admin",
                "url": "http://localhost",
            },
            {
                "name": "uterm_role",
                "value": "admin",
                "url": "http://localhost",
            },
        ]
    )


# ---------------------------------------------------------------------------
# CDN URLs
# ---------------------------------------------------------------------------

_XTERM_CDN = "https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0"
_FIT_CDN = "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0"

SCREENSHOTS_DIR = Path("packages/provide-uterm/tests/playwright/screenshots")


# ---------------------------------------------------------------------------
# ColorWorker — sends ANSI data through TermHub as a fake worker
# ---------------------------------------------------------------------------


class ColorWorker:
    """Background-thread worker that connects to TermHub and sends ANSI color data.

    Sends data repeatedly so the browser always receives it regardless of
    when it connects.
    """

    def __init__(self, base_url: str, worker_id: str, ansi_data: str) -> None:
        self._base_url = base_url
        self._worker_id = worker_id
        self._ansi_data = ansi_data
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> ColorWorker:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._connected.wait(timeout=5.0):
            raise RuntimeError(f"ColorWorker: worker {self._worker_id!r} failed to connect")
        return self

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._connect())
        finally:
            loop.close()

    async def _connect(self) -> None:
        import os

        import websockets

        ws_url = self._base_url.replace("http://", "ws://") + f"/ws/worker/{self._worker_id}/term"
        headers: dict[str, str] = {}
        bearer = os.environ.get("UTERM_TEST_WORKER_BEARER", "").strip()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        try:
            async with websockets.connect(ws_url, additional_headers=headers or None) as ws:
                self._connected.set()
                snapshot = {
                    "type": "snapshot",
                    "screen": self._ansi_data,
                    "cursor": {"x": 0, "y": 0},
                    "cols": 120,
                    "rows": 40,
                    "screen_hash": "color-palette",
                    "cursor_at_end": True,
                    "has_trailing_space": False,
                    "ts": time.time(),
                }
                await ws.send(encode_control_frame(snapshot))

                send_count = 0
                while not self._stop.is_set():
                    if send_count < 5:
                        await ws.send(self._ansi_data)
                        send_count += 1
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=0.5)
                    except TimeoutError:
                        continue
                    except Exception:
                        break
        except Exception:
            self._connected.set()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)


# ---------------------------------------------------------------------------
# AnimatedWorker — sends ANSI animation frames at a target FPS
# ---------------------------------------------------------------------------


class AnimatedWorker:
    """Background-thread worker that streams animation frames through TermHub."""

    def __init__(self, base_url: str, worker_id: str, frames: list[str], fps: float = 10) -> None:
        self._base_url = base_url
        self._worker_id = worker_id
        self._frames = frames
        self._fps = fps
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> AnimatedWorker:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._connected.wait(timeout=5.0):
            raise RuntimeError(f"AnimatedWorker: {self._worker_id!r} failed to connect")
        return self

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._connect())
        finally:
            loop.close()

    async def _connect(self) -> None:
        import os

        import websockets

        ws_url = self._base_url.replace("http://", "ws://") + f"/ws/worker/{self._worker_id}/term"
        headers: dict[str, str] = {}
        bearer = os.environ.get("UTERM_TEST_WORKER_BEARER", "").strip()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        try:
            async with websockets.connect(ws_url, additional_headers=headers or None) as ws:
                self._connected.set()
                snapshot = {
                    "type": "snapshot",
                    "screen": "",
                    "cursor": {"x": 0, "y": 0},
                    "cols": 120,
                    "rows": 40,
                    "screen_hash": "anim-init",
                    "cursor_at_end": True,
                    "has_trailing_space": False,
                    "ts": time.time(),
                }
                await ws.send(encode_control_frame(snapshot))

                delay = 1.0 / self._fps
                # Loop frames until stopped (cycles for video recording)
                while not self._stop.is_set():
                    for frame in self._frames:
                        if self._stop.is_set():
                            break
                        await ws.send(frame)
                        await asyncio.sleep(delay)
        except Exception:
            self._connected.set()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)


# ---------------------------------------------------------------------------
# Test page HTML
# ---------------------------------------------------------------------------


def _color_test_html(worker_id: str) -> str:
    """Minimal HTML page: xterm.js terminal connected to TermHub browser WS."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Color Palette Test</title>
  <link rel="stylesheet" href="{_XTERM_CDN}/css/xterm.css">
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    html, body {{ width: 100%; height: 100vh; background: #0b0f14; }}
    #term {{ width: 100%; height: 100%; }}
  </style>
</head>
<body>
<div id="term"></div>
<script src="{_XTERM_CDN}/lib/xterm.js"></script>
<script src="{_FIT_CDN}/lib/addon-fit.js"></script>
<script>
(function() {{
  var DLE = "\\x10", STX = "\\x02";
  var term = new Terminal({{ rows: 40, cols: 120, convertEol: false }});
  term.open(document.getElementById("term"));
  var fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  try {{ fit.fit(); }} catch(e) {{}}
  window._term = term;
  window._colorDataReceived = false;

  var proto = location.protocol === "https:" ? "wss:" : "ws:";
  var wsUrl = proto + "//" + location.host + "/ws/browser/{worker_id}/term";
  var ws = new WebSocket(wsUrl);

  ws.onmessage = function(event) {{
    var raw = event.data;
    var pos = 0;
    while (pos < raw.length) {{
      if (raw[pos] === DLE && pos + 1 < raw.length && raw[pos+1] === STX) {{
        var lenHex = raw.substring(pos + 2, pos + 10);
        var jsonLen = parseInt(lenHex, 16);
        var jsonStart = pos + 11;
        var jsonStr = raw.substring(jsonStart, jsonStart + jsonLen);
        try {{
          var msg = JSON.parse(jsonStr);
          if (msg.type === "term" && msg.data) {{
            term.write(msg.data);
            window._colorDataReceived = true;
          }} else if (msg.type === "snapshot" && msg.screen) {{
            term.write(msg.screen.replace(/\\n/g, "\\r\\n"));
            window._colorDataReceived = true;
          }}
        }} catch(e) {{}}
        pos = jsonStart + jsonLen;
      }} else {{
        var next = raw.indexOf(DLE, pos + 1);
        if (next === -1) next = raw.length;
        var chunk = raw.substring(pos, next);
        if (chunk) {{
          term.write(chunk);
          window._colorDataReceived = true;
        }}
        pos = next;
      }}
    }}
  }};

  ws.onopen = function() {{ window._wsConnected = true; }};
  ws.onerror = function(e) {{ console.error("WS error", e); }};
}})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# color_server fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def color_server() -> Generator[str, None, None]:
    """Module-scoped color server (in-process Python, or multi-backend subprocess)."""
    import os

    from .ui_routes import multi_backend_env

    if multi_backend_env():
        from .backend_server import WORKER_BEARER, spawn_backend_server

        os.environ["UTERM_TEST_WORKER_BEARER"] = WORKER_BEARER
        with spawn_backend_server() as srv:
            yield srv.base_url
        return

    hub = TermHub(resolve_browser_role=lambda _ws, _worker_id: "admin")
    app = FastAPI()
    app.include_router(hub.create_router())

    @app.get("/color-test/{worker_id}", response_class=HTMLResponse)
    async def color_test_page(worker_id: str) -> str:
        return _color_test_html(worker_id)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("color_server: uvicorn failed to start within 10 s")
        time.sleep(0.05)

    yield f"http://127.0.0.1:{port}"

    stop_uvicorn_thread(server, thread)


# ---------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------


def wait_for_color_data(page: Page) -> None:
    """Wait for xterm.js to receive and render color data."""
    page.wait_for_selector(".xterm-rows", timeout=10000)
    page.wait_for_function("window._colorDataReceived === true", timeout=15000)
    page.wait_for_timeout(500)


def assert_has_colored_spans(page: Page) -> None:
    """Assert xterm DOM contains styled spans (proof of color rendering).

    xterm.js DOM renderer uses CSS classes for 256-color and inline
    styles for truecolor.
    """
    colored = page.evaluate("""() => {
        const rows = document.querySelector('.xterm-rows');
        if (!rows) return 0;
        const spans = rows.querySelectorAll('span');
        let count = 0;
        for (const s of spans) {
            if (s.style.backgroundColor || s.style.color) { count++; continue; }
            if (/xterm-(fg|bg)-\\d+/.test(s.className)) { count++; continue; }
        }
        return count;
    }""")
    assert colored > 0, f"Expected colored spans in xterm DOM, found {colored}"
