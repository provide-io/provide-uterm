#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: Send an image URL to the shell render command, get ANSI truecolor art back."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from pathlib import Path

from scripts.demos import (
    BASE_OUT,
    BrowserStep,
    asciinema_record,
    banner,
    browser_record,
    free_port,
    info,
    ok,
    out_dir,
    start_server,
    stop_server,
    trim_clip,
    wait_for_terminal,
)

FEATURE = "shell_render"
DESCRIPTION = "Send an image URL to the shell render command, get ANSI truecolor art back"
TITLE = "Shell Rendering"
SUBTITLE = "ANSI color, Unicode, box-drawing"
HIGHLIGHT_START_S: float = 3.0
HIGHLIGHT_DURATION_S: float = 6.0

# 4x4 red PNG (valid, minimal — generated via struct/zlib)
_RED_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000004000000040802000000269309290000001049444154"
    "789c63f8cfc000470cc47100ae930ff1d05f239e0000000049454e44ae426082"
)


def _start_image_server() -> tuple[HTTPServer, int]:
    """Start a minimal HTTP server that serves the test PNG."""

    class _H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(_RED_PNG)

        def log_message(self, *_: object) -> None:
            pass

    port = free_port()
    srv = HTTPServer(("127.0.0.1", port), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


async def run_terminal_demo() -> None:
    """Run the shell render feature demo."""
    img_server, img_port = _start_image_server()
    img_url = f"http://127.0.0.1:{img_port}/image.png"

    base_url, server = start_server(
        sessions=[
            {
                "session_id": "provide-shell",
                "display_name": "Provide Shell",
                "connector_type": "ushell",
                "input_mode": "open",
                "auto_start": True,
                "tags": ["shell", "ushell"],
            }
        ]
    )
    time.sleep(1.5)

    banner(DESCRIPTION)

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as http:
        # Start inline PNG server
        info("Starting inline PNG server...")
        ok(f"PNG server at http://127.0.0.1:{img_port}/image.png")

        # Fetch initial snapshot dimensions
        info("Fetching session snapshot...")
        r = await http.get("/api/sessions/provide-shell/snapshot")
        r.raise_for_status()
        snapshot_before = r.json() or {}
        cols = snapshot_before.get("cols")
        rows = snapshot_before.get("rows")
        kv("cols", cols)
        kv("rows", rows)

        # Switch provide-shell to hijack input mode (required for REST hijack)
        await http.patch("/api/sessions/provide-shell", json={"input_mode": "hijack"})
        time.sleep(0.5)

        # Acquire a hijack lease on the default shell
        r = await http.post(
            "/worker/provide-shell/hijack/acquire",
            json={"owner": "operator", "lease_s": 30},
        )
        r.raise_for_status()
        hijack_id = r.json().get("hijack_id", "")

        info(f"render {png_url}")
        r = await http.post(
            f"/worker/provide-shell/hijack/{hijack_id}/send",
            json={"keys": f"render {img_url}\r"},
        )
        r.raise_for_status()
        ok("render command sent")

        info(f"render --loop {gif_url}")
        r = await http.post(
            f"/worker/provide-shell/hijack/{hijack_id}/send",
            json={"keys": f"render --loop {gif_url}\r"},
        )
        r.raise_for_status()
        ok("animated render running — 12-frame color wheel, looping at 60ms/frame")
        time.sleep(3.0)

        # Inspect the updated snapshot
        info("Waiting for ANSI art in snapshot...")
        r = await http.get("/api/sessions/provide-shell/snapshot")
        snapshot_after = r.json() or {}
        cols_after = snapshot_after.get("cols")
        rows_after = snapshot_after.get("rows")
        screen_text = snapshot_after.get("screen", "")
        has_ansi = "\x1b[" in screen_text
        if has_ansi or (cols_after != cols or rows_after != rows):
            ok(f"snapshot updated: cols={cols_after} rows={rows_after}")
        else:
            warn("snapshot unchanged")

        # Release the hijack lease
        await http.post(f"/worker/provide-shell/hijack/{hijack_id}/release")

    info("(type 'render <url>' in the ushell to render any image as ANSI art)")
    ok("Shell render demo complete")
    img_server.shutdown()
    stop_server(server)


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Record asciinema cast + browser screenshots for the shell render demo."""
    feat_dir = out_dir(FEATURE, base_out)

    # Record terminal demo with asciinema
    cast_path = asciinema_record(__file__, feat_dir / "terminal.cast")

    # Start image server + provide-terminal server for browser recording.
    # Use the "ushell" connector so the real CommandDispatcher (with render/cast)
    # runs in-process — the default "shell" connector is only a simulated transcript.
    img_server, img_port = _start_image_server()
    png_url = f"http://127.0.0.1:{img_port}/image.png"
    gif_url = f"http://127.0.0.1:{img_port}/animation.gif"
    base_url, server = start_server(
        sessions=[
            {
                "session_id": "provide-shell",
                "display_name": "Provide Shell",
                "connector_type": "ushell",
                "input_mode": "open",
                "auto_start": True,
                "tags": ["shell", "ushell"],
            }
        ]
    )
    time.sleep(1.5)

    # Switch to hijack mode and pre-acquire lease so the ushell executes commands
    # directly instead of showing the "Shared input" transcript view.
    hijack_id = ""
    try:
        import httpx as _httpx

        with _httpx.Client(base_url=base_url, timeout=10.0) as http:
            http.patch("/api/sessions/provide-shell", json={"input_mode": "hijack"})
            r = http.post(
                "/worker/provide-shell/hijack/acquire",
                json={"owner": "operator", "lease_s": 120},
            )
            if r.status_code == 200:
                hijack_id = r.json().get("hijack_id", "")
    except Exception as exc:
        print(f"  [WARN] hijack setup: {exc}", flush=True)

    # JS that hides all UI chrome using structural selectors (React CSS-modules use
    # hashed class names so plain class selectors won't match; we navigate the DOM
    # directly instead).
    _KIOSK_JS = r"""() => {
        const set = (el, css) => { if (el) el.style.cssText += css; };
        const hide = sel => document.querySelectorAll(sel).forEach(el => set(el, ';display:none!important'));

        // Hide top nav bar (<header> element)
        hide('header');

        // The React app renders: .page-shell > [header, div.layout]
        // div.layout is a CSS-grid with [sidebar, main]; collapse the sidebar column.
        const shell = document.querySelector('.page-shell');
        if (shell) {
            const layout = Array.from(shell.children).find(el => el.tagName === 'DIV');
            if (layout) {
                // Collapse sidebar (first grid cell)
                const sidebar = layout.children[0];
                hide_el(sidebar);
                // Expand main (second grid cell) to full width
                set(layout, ';grid-template-columns:1fr!important;display:flex!important;flex-direction:column!important;');
                const main = layout.children[1];
                if (main) {
                    set(main, ';flex:1!important;min-height:0!important;display:flex!important;flex-direction:column!important;');
                    // Hide status bar (last child of main)
                    set(main.lastElementChild, ';display:none!important');
                }
            }
            set(shell, ';height:100vh!important;display:flex!important;flex-direction:column!important;overflow:hidden!important;');
        }

        // Hide hijack chrome (non-hashed stable class names)
        hide('.hijack-toolbar');
        hide('.hijack-input-row');
        hide('.mobile-keys');
        hide('.hijack-analysis');

        // Expand terminal widget to fill remaining space
        document.querySelectorAll('.provide-hijack').forEach(el =>
            set(el, ';height:100%!important;flex:1!important;min-height:0!important;display:flex!important;flex-direction:column!important;'));
        document.querySelectorAll('.hijack-terminal').forEach(el =>
            set(el, ';flex:1!important;min-height:0!important;height:100%!important;'));

        function hide_el(el) { if (el) set(el, ';display:none!important'); }
    }"""

    def _full_terminal(page: object) -> None:
        """Run JS to hide all UI chrome and fill viewport with the xterm terminal."""
        import contextlib

        with contextlib.suppress(Exception):
            page.evaluate(_KIOSK_JS)  # type: ignore[union-attr]

    def _send_render(page: object) -> None:  # noqa: ARG001
        if not hijack_id:
            return
        import httpx as _h

        with _h.Client(base_url=base_url, timeout=10.0) as http:
            http.post(f"/worker/provide-shell/hijack/{hijack_id}/send", json={"keys": f"render {png_url}\r"})

    def _send_animated(page: object) -> None:  # noqa: ARG001
        if not hijack_id:
            return
        import httpx as _h

        with _h.Client(base_url=base_url, timeout=10.0) as http:
            http.post(f"/worker/provide-shell/hijack/{hijack_id}/send", json={"keys": f"render --loop {gif_url}\r"})

    steps: list[BrowserStep] = [
        # Navigate to operator view (WebSocket connects here)
        ("/app/operator/provide-shell", 1.0, None),
        (lambda p: wait_for_terminal(p), 0.3, None),
        # Hide all chrome so xterm fills the viewport
        (_full_terminal, 0.3, None),
        # Send render command while browser WebSocket is connected
        (_send_render, 4.5, "01-rainbow-render.png"),
        # Send animated GIF render (loops)
        (_send_animated, 5.0, "02-animated-render.png"),
    ]
    mp4_path = browser_record(base_url, steps, feat_dir)

    # Release hijack lease now that recording is done
    if hijack_id:
        try:
            import httpx as _h

            with _h.Client(base_url=base_url, timeout=10.0) as http:
                http.post(f"/worker/provide-shell/hijack/{hijack_id}/release")
        except Exception:
            pass

    img_server.shutdown()
    stop_server(server)

    highlight = trim_clip(mp4_path, HIGHLIGHT_START_S, HIGHLIGHT_DURATION_S)
    return {"cast": cast_path, "mp4": mp4_path, "highlight": highlight}


if __name__ == "__main__":
    if "--run-demo" in sys.argv:
        asyncio.run(run_terminal_demo())
    else:
        result = record()
        print(f"\nShell render demo: {result}")
