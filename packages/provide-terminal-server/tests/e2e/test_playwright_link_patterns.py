#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Playwright e2e: server-pushed ``link_patterns`` ControlChannel feature.

Proves end-to-end that:
  1. An HTML page loads xterm.js from CDN + our ``xterm-server-links.js`` module
     (served over local HTTP so CDN + WS connections both work).
  2. The page opens a WebSocket to a locally-controlled test WS server.
  3. The server emits terminal data followed by a ``link_patterns`` control frame
     with a ``sector-nav`` pattern (``\\((\\d{1,5})\\)``, action=cmd, payload=``$1\\r``).
  4. The matched text ``4521`` is registered as a clickable xterm link.
  5. Activating that link causes the browser to send ``4521\\r`` back over WS.

Compromise note: pure DOM mouse-click on the xterm canvas is impractical
(link providers fire via internal xterm pointer events, not standard DOM events).
Instead the page exposes ``window.__testActivateLink(text)`` — a test-only hook
that calls the link provider's ``activate()`` handler directly.  Everything else
is genuine: real Chromium headless, real xterm.js (CDN), real XtermServerLinks
module (from disk), real WebSocket round-trip.  Video is recorded to
``/tmp/playwright-link-patterns/`` as proof-of-work.

Skip: module is skipped cleanly if playwright is not installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

# Skip the entire module cleanly if playwright is not installed.
playwright_mod = pytest.importorskip("playwright")

import websockets  # noqa: E402  — after importorskip

from playwright.sync_api import Page, sync_playwright  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# packages/provide-terminal-server/tests/e2e/  → parents[4] = repo root
_REPO_ROOT = Path(__file__).resolve().parents[4]
_XTERM_SERVER_LINKS_JS = (
    _REPO_ROOT
    / "packages"
    / "provide-terminal"
    / "src"
    / "provide"
    / "terminal"
    / "frontend"
    / "xterm-server-links.js"
)

_VIDEO_DIR = Path("/tmp/playwright-link-patterns")

# ---------------------------------------------------------------------------
# ControlChannel helpers (inlined — no server-package imports needed)
# ---------------------------------------------------------------------------

_DLE = "\x10"
_STX = "\x02"


def _encode_data(data: str) -> str:
    """Escape DLE chars in plain terminal data."""
    return data.replace(_DLE, _DLE + _DLE)


def _encode_control(payload: dict[str, Any]) -> str:
    """Wrap a JSON payload in DLE-STX ControlChannel framing."""
    serialized = json.dumps(payload, separators=(",", ":"))
    return f"{_DLE}{_STX}{len(serialized):08x}:{serialized}"


# ---------------------------------------------------------------------------
# Sector-nav link pattern under test
# ---------------------------------------------------------------------------

_SECTOR_NAV_PATTERN: dict[str, Any] = {
    "id": "sector-nav",
    "pattern": r"\((\d{1,5})\)",
    "group": 1,
    "action": "cmd",
    "payload": "$1\r",
    "class": "sector",
}

_LINK_PATTERNS_FRAME: dict[str, Any] = {
    "type": "link_patterns",
    "patterns": [_SECTOR_NAV_PATTERN],
}

# ---------------------------------------------------------------------------
# HTML page template — xterm.js CDN + inline xterm-server-links.js
# ---------------------------------------------------------------------------


def _build_page_html() -> str:
    """Return the test HTML page.

    xterm-server-links.js is loaded as an EXTERNAL script resource
    (served by _PageServer at /xterm-server-links.js) — NOT inlined.
    The module's own doc comment contains a ``</script>`` example which
    would prematurely close an inlining-style ``<script>`` block.
    """
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>link_patterns e2e test</title>
  <link rel="stylesheet"
        href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.css"/>
</head>
<body style="background:#000;margin:0;padding:8px;">
  <div id="terminal"></div>
  <div id="status" style="color:#0f0;font-family:monospace;margin-top:8px;">init</div>

  <!-- xterm.js from CDN -->
  <script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.js"></script>
  <!-- xterm-server-links.js — served by the local HTTP fixture. -->
  <script src="/xterm-server-links.js"></script>
  <script>
    // ── Test-observable state ───────────────────────────────────────────────
    window.__sentFrames    = [];   // payloads sent browser→server
    window.__receivedData  = [];   // plain terminal data received
    window.__linkActivated = null; // set to payload string when activate fires

    // ── xterm terminal ──────────────────────────────────────────────────────
    var term = new Terminal({{ cols: 80, rows: 24, cursorBlink: false }});
    term.open(document.getElementById('terminal'));

    // ── Locate the on-screen pixel of matching text ────────────────────────
    // Returns {{x, y, width, height}} of the centre of the first occurrence of
    // *text* in the xterm buffer, in page (viewport) coordinates. The driver
    // uses this to compute where to point the real mouse so xterm's own
    // pointer→link-provider pipeline is exercised end-to-end.
    window.__findTextPx = function(text) {{
      var buf = term.buffer && term.buffer.active;
      if (!buf) return null;
      for (var row = 0; row < term.rows; row++) {{
        var line = buf.getLine(row);
        if (!line) continue;
        var lineText = line.translateToString(true);
        var col = lineText.indexOf(text);
        if (col < 0) continue;
        var el = term.element;
        if (!el) return null;
        // Use the xterm screen element if present — it sits flush against
        // the grid with no scrollbar padding, so cellW/cellH are accurate.
        var screen = el.querySelector('.xterm-screen') || el;
        var rect = screen.getBoundingClientRect();
        var cellW = rect.width / term.cols;
        var cellH = rect.height / term.rows;
        return {{
          x: rect.left + (col + text.length / 2) * cellW,
          y: rect.top + (row + 0.5) * cellH,
          width: cellW * text.length,
          height: cellH,
        }};
      }}
      return null;
    }};

    // ── WebSocket connection (called by the test once the page is ready) ───
    var ws = null;

    window.__connectWS = function(url) {{
      ws = new WebSocket(url);
      ws.onopen = function() {{
        document.getElementById('status').textContent = 'ws:open';
      }};
      ws.onclose = function() {{
        var el = document.getElementById('status');
        if (el) el.textContent = 'ws:closed';
      }};
      ws.onmessage = function(e) {{
        var raw = e.data;
        // Detect ControlChannel frame: DLE(0x10) STX(0x02) 8hexdigits : json
        if (raw.charCodeAt(0) === 0x10 && raw.charCodeAt(1) === 0x02) {{
          var colon = raw.indexOf(':', 2);
          if (colon === -1) return;
          var jsonStr = raw.slice(colon + 1);
          try {{
            var msg = JSON.parse(jsonStr);
            if (msg.type === 'link_patterns') {{
              serverLinks.update(msg.patterns);
              document.getElementById('status').textContent = 'link_patterns:applied';
            }}
          }} catch(_e) {{}}
        }} else {{
          // Plain terminal data — write to xterm.
          window.__receivedData.push(raw);
          term.write(raw);
        }}
      }};
    }};

    // ── XtermServerLinks integration ────────────────────────────────────────
    // onActivate: send the substituted payload back over WS.
    var serverLinks = window.XtermServerLinks.register(term, {{
      onActivate: function(action, payload, match) {{
        window.__linkActivated = payload;
        if (ws && ws.readyState === WebSocket.OPEN) {{
          ws.send(payload);
          window.__sentFrames.push(payload);
        }}
      }},
    }});
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Minimal local HTTP server to serve the test page
# ---------------------------------------------------------------------------


class _PageServer:
    """HTTP server serving the test HTML on ``/`` and the
    xterm-server-links.js module on ``/xterm-server-links.js``.

    The JS is served as a separate resource — NOT inlined — because
    the module's doc comment legitimately contains a ``</script>``
    example which, when inlined into a page's own ``<script>`` block,
    causes the browser's HTML parser to terminate the script early.
    """

    def __init__(self, html: str, js_source: str) -> None:
        self._html = html.encode()
        self._js = js_source.encode()
        self.host = "127.0.0.1"
        self.port = _free_port()
        self._httpd: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        html_bytes = self._html
        js_bytes = self._js

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.rstrip("/") in ("", "/"):
                    body, ctype = html_bytes, "text/html; charset=utf-8"
                elif self.path == "/xterm-server-links.js":
                    body, ctype = js_bytes, "application/javascript; charset=utf-8"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:  # suppress request logs
                pass

        self._httpd = HTTPServer((self.host, self.port), _Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"


# ---------------------------------------------------------------------------
# Local WebSocket server (receives terminal data requests from browser)
# ---------------------------------------------------------------------------


class _WsTestServer:
    """Asyncio WS server that:

    1. On each new connection: sends terminal data + link_patterns control frame.
    2. Collects all messages sent by the browser (expected: the link payload).
    """

    def __init__(self) -> None:
        self.received: list[str] = []
        self.host = "127.0.0.1"
        self.port = _free_port()
        self._server: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    async def _handler(self, ws: Any) -> None:
        # 1. Terminal data — plain text, with DLE-escaping.
        await ws.send(_encode_data("Warp to (4521) now.\r\n"))
        # 2. link_patterns control frame.
        await ws.send(_encode_control(_LINK_PATTERNS_FRAME))
        # 3. Drain browser replies.
        try:
            async for msg in ws:
                self.received.append(msg)
        except Exception:
            pass

    async def _serve(self) -> None:
        self._server = await websockets.serve(self._handler, self.host, self.port)
        self._ready.set()
        await asyncio.Future()  # block until task is cancelled

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            self._loop.close()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def stop(self) -> None:
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Return an OS-assigned free TCP port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.playwright
def test_link_patterns_browser_sends_payload_on_activation() -> None:
    """Prove link_patterns ControlChannel end-to-end in a real Chromium browser.

    Flow:
      1. Start a local HTTP server that serves the test HTML.
      2. Start a local WS server ready to push terminal data + link_patterns.
      3. Open a real Chromium page and navigate to the HTTP fixture.
      4. Connect the page WS to the local server.
      5. Server sends: terminal data "Warp to (4521) now." + link_patterns frame.
      6. Test calls window.__testActivateLink('4521') to trigger the link.
      7. Assert browser sends '4521\\r' back to the WS server.

    Compromise: step 6 uses a test-only JS hook instead of a physical mouse
    click, because xterm renders to canvas and link clicks are internal to xterm.
    See module docstring for full rationale.
    """
    import time

    assert _XTERM_SERVER_LINKS_JS.exists(), f"JS source not found: {_XTERM_SERVER_LINKS_JS}"
    js_source = _XTERM_SERVER_LINKS_JS.read_text()

    html = _build_page_html()

    page_server = _PageServer(html, js_source)
    page_server.start()

    ws_server = _WsTestServer()
    ws_server.start()

    headed = os.environ.get("HEADED", "").lower() in ("1", "true", "yes")
    _VIDEO_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        ctx = browser.new_context(
            record_video_dir=str(_VIDEO_DIR),
            record_video_size={"width": 1024, "height": 600},
        )
        page: Page = ctx.new_page()

        try:
            # Capture console messages for debugging.
            console_msgs: list[str] = []
            page.on("console", lambda m: console_msgs.append(f"[{m.type}] {m.text}"))
            page.on("pageerror", lambda e: console_msgs.append(f"[pageerror] {e}"))

            # Navigate to the local HTTP page (CDN + WS both work over http://).
            page.goto(page_server.url, wait_until="load", timeout=30_000)

            # Debug: dump what JS globals exist.
            has_fn = page.evaluate("() => typeof window.__connectWS")
            has_xterm = page.evaluate("() => typeof window.Terminal")
            has_links = page.evaluate("() => typeof window.XtermServerLinks")
            print(f"DEBUG: __connectWS={has_fn}, Terminal={has_xterm}, XtermServerLinks={has_links}")
            for m in console_msgs:
                print(f"CONSOLE: {m}")

            # Connect the page's WebSocket to the test WS server.
            page.evaluate(f"window.__connectWS({ws_server.ws_url!r})")

            # Wait for WS handshake OR for link_patterns frame to arrive —
            # the server sends both on connect, so the status text races ahead
            # of the test if we pin on the intermediate 'ws:open' value.
            page.wait_for_function(
                "() => ['ws:open','link_patterns:applied'].includes("
                "document.getElementById('status').textContent)",
                timeout=10_000,
            )

            # Wait for terminal data to arrive.
            page.wait_for_function(
                "() => (window.__receivedData || []).length > 0",
                timeout=10_000,
            )

            # Wait for link_patterns frame to be processed.
            page.wait_for_function(
                "() => document.getElementById('status').textContent === 'link_patterns:applied'",
                timeout=10_000,
            )

            # Allow xterm's link provider re-run (triggered by serverLinks.update → term.refresh).
            page.wait_for_timeout(600)

            # Real DOM mouse click through xterm's pointer → link-provider
            # pipeline. We ask the page for the pixel centre of "4521" as
            # rendered in the xterm buffer, then drive the real mouse to
            # that location. xterm handles the click via registerLinkProvider
            # just as if a user had pointed and clicked.
            coords = page.evaluate("() => window.__findTextPx('4521')")
            assert coords, "could not find '4521' rendered in xterm buffer"
            # Hover first — some xterm builds only arm the link provider after
            # a mousemove resolves which cell is under the pointer.
            page.mouse.move(coords["x"], coords["y"])
            page.wait_for_timeout(100)
            page.mouse.click(coords["x"], coords["y"])

            # Wait for browser to send the payload.
            page.wait_for_function(
                "() => (window.__sentFrames || []).length > 0",
                timeout=8_000,
            )

            sent: list[str] = page.evaluate("() => window.__sentFrames")
            assert sent, "Browser sent no WS frames after link activation"
            assert sent[0] == "4521\r", f"Expected '4521\\r'; got {sent[0]!r}"

            # Verify the WS server also received the payload.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not ws_server.received:
                time.sleep(0.05)

            assert ws_server.received, "WS server received no messages from browser"
            assert ws_server.received[0] == "4521\r", (
                f"WS server expected '4521\\r'; got {ws_server.received[0]!r}"
            )

        finally:
            page_server.stop()
            ws_server.stop()
            ctx.close()
            browser.close()

    # Report video path (proof-of-work).
    videos = list(_VIDEO_DIR.glob("*.webm"))
    if videos:
        newest = max(videos, key=lambda p: p.stat().st_mtime)
        print(f"\nVideo recorded: {newest}")
