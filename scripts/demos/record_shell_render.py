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
    kv,
    ok,
    out_dir,
    send_to_session,
    start_server,
    stop_server,
    trim_clip,
    wait_for_terminal,
    warn,
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

    base_url, server = start_server()
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

        # Send the render command
        info("Sending render command via hijack...")
        info(f"  render {img_url}")
        r = await http.post(
            f"/worker/provide-shell/hijack/{hijack_id}/send",
            json={"keys": f"render {img_url}\r"},
        )
        r.raise_for_status()
        ok("render command sent")

        # Wait for render to complete
        time.sleep(2.0)

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

    info("(ANSI truecolor art rendered in browser terminal — see browser recording)")
    ok("Shell render complete — image converted to ANSI art")
    img_server.shutdown()
    stop_server(server)


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Record asciinema cast + browser screenshots for the shell render demo."""
    feat_dir = out_dir(FEATURE, base_out)

    # Record terminal demo with asciinema
    cast_path = asciinema_record(__file__, feat_dir / "terminal.cast")

    # Start image server + provide-terminal server for browser recording
    img_server, img_port = _start_image_server()
    img_url = f"http://127.0.0.1:{img_port}/image.png"
    base_url, server = start_server()
    time.sleep(1.5)

    # Run the render command before the browser connects so the terminal shows ANSI art
    send_to_session(base_url, "provide-shell", f"render {img_url}\r", wait_s=2.0)

    steps: list[BrowserStep] = [
        ("/app/session/provide-shell", 0.5, None),
        (lambda p: wait_for_terminal(p), 3.0, "01-shell-render-output.png"),
        ("/app/operator/provide-shell", 0.5, None),
        (lambda p: wait_for_terminal(p), 2.0, "02-operator-view.png"),
    ]
    mp4_path = browser_record(base_url, steps, feat_dir)

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
