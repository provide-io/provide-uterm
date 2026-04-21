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
SUBTITLE = "ANSI truecolor art from any image URL"
HIGHLIGHT_START_S: float = 3.0
HIGHLIGHT_DURATION_S: float = 8.0


def _make_rainbow_png(width: int = 160, height: int = 60) -> bytes:
    """Generate a vibrant rainbow gradient PNG with PIL."""
    import io

    from PIL import Image

    img = Image.new("RGB", (width, height))
    pixels = img.load()
    for x in range(width):
        hue = x / width
        h6 = hue * 6.0
        i = int(h6)
        f = h6 - i
        q, t = int(255 * (1.0 - f)), int(255 * f)
        rgb_map = [(255, t, 0), (q, 255, 0), (0, 255, t), (0, q, 255), (t, 0, 255), (255, 0, q)]
        r, g, b = rgb_map[i % 6]
        for y in range(height):
            brightness = 0.55 + 0.45 * (y / height)
            pixels[x, y] = (int(r * brightness), int(g * brightness), int(b * brightness))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


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
    """Start an HTTP server serving a static rainbow PNG and animated GIF."""
    rainbow_png = _make_rainbow_png()
    spin_gif = _make_spin_gif()

    class _H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.endswith(".gif"):
                data, ct = spin_gif, "image/gif"
            else:
                data, ct = rainbow_png, "image/png"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_: object) -> None:
            pass

    port = free_port()
    srv = HTTPServer(("127.0.0.1", port), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


async def run_terminal_demo() -> None:
    """Run the shell render feature demo."""
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

    banner(DESCRIPTION)

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as http:
        info("Starting image server (rainbow PNG + animated GIF)...")
        ok(f"  PNG: {png_url}")
        ok(f"  GIF: {gif_url}")

        # Switch provide-shell to hijack input mode
        await http.patch("/api/sessions/provide-shell", json={"input_mode": "hijack"})
        time.sleep(0.3)

        r = await http.post(
            "/worker/provide-shell/hijack/acquire",
            json={"owner": "operator", "lease_s": 60},
        )
        r.raise_for_status()
        hijack_id = r.json().get("hijack_id", "")

        info(f"render {png_url}")
        r = await http.post(
            f"/worker/provide-shell/hijack/{hijack_id}/send",
            json={"keys": f"render {png_url}\r"},
        )
        r.raise_for_status()
        ok("static render complete — rainbow gradient as ANSI truecolor art")
        time.sleep(2.5)

        info(f"render --loop {gif_url}")
        r = await http.post(
            f"/worker/provide-shell/hijack/{hijack_id}/send",
            json={"keys": f"render --loop {gif_url}\r"},
        )
        r.raise_for_status()
        ok("animated render running — 12-frame color wheel, looping at 60ms/frame")
        time.sleep(3.0)

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

    def _send_render(page: object) -> None:
        if not hijack_id:
            return
        import httpx as _h

        with _h.Client(base_url=base_url, timeout=10.0) as http:
            http.post(f"/worker/provide-shell/hijack/{hijack_id}/send", json={"keys": f"render {png_url}\r"})

    def _send_animated(page: object) -> None:
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
