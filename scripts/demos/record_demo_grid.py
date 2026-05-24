#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: 9-terminal grid — ANSI render animations running simultaneously in a 3x3 layout."""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from scripts.demos import (
    BASE_OUT,
    BrowserStep,
    browser_record,
    dev_bearer_headers,
    free_port,
    out_dir,
    start_server,
    stop_server,
    trim_clip,
)

FEATURE = "demo_grid"
DESCRIPTION = "9-terminal grid — ANSI render animations running simultaneously"
TITLE = "Terminal Grid"
SUBTITLE = "9 live terminals in a 3x3 grid"
HIGHLIGHT_START_S: float = 2.0
HIGHLIGHT_DURATION_S: float = 8.0

# Number of sessions / grid size
_N = 9
_COLS = 3
_ROWS = 3

# Session IDs used for the grid
GRID_SESSION_IDS = [f"grid-shell-{i}" for i in range(_N)]


def _make_spin_gif(width: int = 120, height: int = 45, n_frames: int = 12) -> bytes:
    """Generate an animated color-wheel GIF with PIL."""
    import io

    from PIL import Image

    frames = []
    for fi in range(n_frames):
        img = Image.new("RGB", (width, height))
        p = img.load()
        offset = fi / n_frames
        for x in range(width):
            hue = ((x / width) + offset) % 1.0
            h6 = hue * 6.0
            i = int(h6)
            f = h6 - i
            q, t = int(255 * (1.0 - f)), int(255 * f)
            rgb_map = [(255, t, 0), (q, 255, 0), (0, 255, t), (0, q, 255), (t, 0, 255), (255, 0, q)]
            r, g, b = rgb_map[i % 6]
            for y in range(height):
                p[x, y] = (r, g, b)
        frames.append(img)
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=60, loop=0)
    return buf.getvalue()


def _start_image_server() -> tuple[HTTPServer, int]:
    """Start an HTTP server serving animated GIFs with different phase offsets."""
    spin_gif = _make_spin_gif()

    class _H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "image/gif")
            self.end_headers()
            self.wfile.write(spin_gif)

        def log_message(self, *_: object) -> None:
            pass

    port = free_port()
    srv = HTTPServer(("127.0.0.1", port), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


_XTERM_CDN = "https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0"
_FITADDON_CDN = "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0"


def _build_grid_html(session_ids: list[str], cols: int, rows: int, auth_token: str = "") -> str:
    """Build the full 9-terminal grid HTML including hijack.js script tags.

    Uses root-relative URLs so the page works when served from the terminal
    server via page.route() — keeping the correct origin for WebSocket construction.
    xterm.js is loaded as a UMD global (window.Terminal) from CDN before hijack.js.

    ``auth_token`` is embedded into each ProvideHijack widget's ``authToken``
    config; hijack-websocket.ts appends it as ``?token=...`` to the WS URL so
    the server's JWT validator accepts the browser connection. Without this,
    every ProvideHijack WS hits the JWT validator with no credential and the
    page renders 9 empty cells.
    """
    cells_html = "\n  ".join(f'<div class="cell" id="cell-{i}"></div>' for i in range(cols * rows))
    sessions_json = str(session_ids).replace("'", '"')
    auth_json = json.dumps(auth_token)
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Terminal Grid</title>
<link rel="stylesheet" href="{_XTERM_CDN}/css/xterm.css">
<link rel="stylesheet" href="/_terminal/hijack.css">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{
    background: #000;
    width: 100vw;
    height: 100vh;
    overflow: hidden;
  }}
  #grid {{
    display: grid;
    grid-template-columns: repeat({cols}, 1fr);
    grid-template-rows: repeat({rows}, 1fr);
    width: 100%;
    height: 100%;
    gap: 2px;
    background: #111;
  }}
  .cell {{ overflow: hidden; position: relative; display: flex; flex-direction: column; }}
  .cell .provide-hijack {{ flex: 1; min-height: 0; display: flex; flex-direction: column; }}
  .cell .hijack-toolbar {{ display: none !important; }}
  .cell .hijack-input-row {{ display: none !important; }}
  .cell .mobile-keys {{ display: none !important; }}
  .cell .hijack-analysis {{ display: none !important; }}
  .cell .hijack-terminal {{ flex: 1 !important; min-height: 0 !important; }}
  .cell .xterm, .cell .xterm-viewport, .cell .xterm-screen {{ height: 100% !important; }}
</style>
</head>
<body>
<div id="grid">
  {cells_html}
</div>
<!-- xterm UMD must load before hijack.js (which uses window.Terminal) -->
<script src="{_XTERM_CDN}/lib/xterm.js"></script>
<script src="{_FITADDON_CDN}/lib/addon-fit.js"></script>
<script type="module">
  import {{ ProvideHijack }} from '/_terminal/hijack.js';
  const sessions = {sessions_json};
  const authToken = {auth_json};
  const cells = document.querySelectorAll('.cell');
  sessions.forEach(function(sid, i) {{
    new ProvideHijack(cells[i], {{ workerId: sid, authToken: authToken }});
  }});
</script>
</body>
</html>"""


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Record a 3x3 grid of terminals all running simultaneous ANSI animations."""
    feat_dir = out_dir(FEATURE, base_out)

    # Start image server and provide-uterm server with 9 ushell sessions.
    img_server, img_port = _start_image_server()
    gif_url = f"http://127.0.0.1:{img_port}/animation.gif"

    sessions_cfg = [
        {
            "session_id": sid,
            "display_name": f"Terminal {i}",
            "connector_type": "ushell",
            "input_mode": "open",
            "auto_start": True,
            "tags": ["grid", "demo"],
        }
        for i, sid in enumerate(GRID_SESSION_IDS)
    ]
    base_url, server = start_server(sessions=sessions_cfg)
    time.sleep(3.0)  # allow all 9 ushell sessions to establish WS connections

    # Switch each session to hijack mode then pre-acquire the lease before Playwright opens.
    # Retry up to 3 times with 1s delay to handle race conditions on startup.
    hijack_ids: dict[str, str] = {}
    try:
        import httpx as _httpx

        with _httpx.Client(base_url=base_url, timeout=15.0, headers=dev_bearer_headers()) as http:
            for attempt in range(3):
                for sid in GRID_SESSION_IDS:
                    if sid in hijack_ids:
                        continue
                    http.patch(f"/api/sessions/{sid}", json={"input_mode": "hijack"})
                    r = http.post(
                        f"/worker/{sid}/hijack/acquire",
                        json={"owner": "grid-demo", "lease_s": 120},
                    )
                    if r.status_code == 200:
                        hijack_ids[sid] = r.json().get("hijack_id", "")
                if len(hijack_ids) == _N:
                    break
                if attempt < 2:
                    time.sleep(1.0)
            if len(hijack_ids) < _N:
                print(f"  [WARN] only {len(hijack_ids)}/{_N} hijack IDs acquired", flush=True)
    except Exception as exc:
        print(f"  [WARN] hijack setup: {exc}", flush=True)

    # Build full grid HTML — served via page.route() at /demo-grid so the page
    # keeps the correct server origin and WebSocket construction uses location.host.
    # Extract the raw JWT from the bearer header so it can be embedded in the
    # grid page. ProvideHijack passes it to the WS handshake as ``?token=...``.
    _bearer = dev_bearer_headers().get("Authorization", "")
    _grid_auth_token = _bearer.removeprefix("Bearer ").strip()
    grid_html = _build_grid_html(GRID_SESSION_IDS, _COLS, _ROWS, _grid_auth_token)
    grid_html_bytes = grid_html.encode()

    def _register_grid_route(page: object) -> None:
        """Register a route so that GET /_demo/grid returns our custom HTML."""
        import contextlib

        def _fulfill(route: object) -> None:  # type: ignore[misc]
            with contextlib.suppress(Exception):
                route.fulfill(  # type: ignore[union-attr]
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=grid_html_bytes,
                )

        with contextlib.suppress(Exception):
            page.route("**/_demo/grid", _fulfill)  # type: ignore[union-attr]

    def _wait_terminals(page: object) -> None:
        """Wait until at least one xterm span is visible (indicates widgets connected)."""
        import contextlib

        with contextlib.suppress(Exception):
            page.wait_for_selector(  # type: ignore[union-attr]
                ".hijack-terminal .xterm-rows span",
                state="attached",
                timeout=15000,
            )

    def _send_animations(page: object) -> None:
        """Send looping GIF render to all 9 terminals via hijack endpoints."""
        import httpx as _h

        with _h.Client(base_url=base_url, timeout=10.0, headers=dev_bearer_headers()) as http:
            for sid, hid in hijack_ids.items():
                http.post(f"/worker/{sid}/hijack/{hid}/send", json={"keys": f"render --loop {gif_url}\r"})

    steps: list[BrowserStep] = [
        # Register route BEFORE any navigation so the interceptor is in place
        (_register_grid_route, 0.0, None),
        # Navigate to the routed path — keeps server origin for WS and relative URLs
        (base_url + "/_demo/grid", 2.0, None),
        # Wait for at least one xterm span to appear (signals WebSocket connected)
        (_wait_terminals, 1.0, None),
        # Start all 9 animations simultaneously, then wait for them to render
        (_send_animations, 7.0, "01-grid-animations.png"),
        # Capture a second screenshot mid-animation
        (None, 4.0, "02-grid-spinning.png"),
    ]
    mp4_path = browser_record(base_url, steps, feat_dir)

    # Release all hijack leases
    try:
        import httpx as _h

        with _h.Client(base_url=base_url, timeout=10.0, headers=dev_bearer_headers()) as http:
            for sid, hid in hijack_ids.items():
                http.post(f"/worker/{sid}/hijack/{hid}/release")
    except Exception:
        pass

    img_server.shutdown()
    stop_server(server)

    highlight = trim_clip(mp4_path, HIGHLIGHT_START_S, HIGHLIGHT_DURATION_S)
    return {"mp4": mp4_path, "highlight": highlight}


if __name__ == "__main__":
    if "--list" in sys.argv:
        print(f"Grid sessions: {GRID_SESSION_IDS}")
    else:
        result = record()
        print(f"\nGrid demo: {result}")
