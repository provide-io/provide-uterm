#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: Proxy HTTP traffic through uterm inspect tunnel, inspect requests/responses in browser."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from typing import TYPE_CHECKING, Any

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

FEATURE = "http_inspect"
DESCRIPTION = "Proxy HTTP traffic through uterm inspect tunnel, inspect requests/responses in browser"
TITLE = "HTTP Inspection"
SUBTITLE = "Intercept and inspect HTTP traffic"
HIGHLIGHT_START_S: float = 3.0
HIGHLIGHT_DURATION_S: float = 6.0


def _start_target_api() -> tuple[int, Any]:
    """Start a small FastAPI target server for the demo."""
    import uvicorn
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/items")
    async def list_items() -> list[dict[str, Any]]:
        return [{"id": 1, "name": "Widget"}, {"id": 2, "name": "Gadget"}]

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "demo-target-api"}

    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 10.0
    while not srv.started:
        if time.monotonic() > deadline:
            raise RuntimeError("Target API did not start")
        time.sleep(0.05)
    return port, srv


async def run_terminal_demo() -> None:
    """Run the HTTP inspect tunnel feature demo."""
    banner(DESCRIPTION)

    info("Starting inline target API server (items + users endpoints)...")
    target_port, target_srv = _start_target_api()
    ok(f"Target API running on http://127.0.0.1:{target_port}")

    base_url, server = start_server()

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        # Create an HTTP inspect tunnel
        info("Creating HTTP inspect tunnel via /api/tunnels...")
        r = await client.post(
            "/api/tunnels",
            json={"tunnel_type": "http", "display_name": "demo-inspect", "local_port": target_port},
        )
        if r.status_code == 404:
            warn("/api/tunnels returned 404 — tunnel route not available in this config")
        else:
            r.raise_for_status()
            tunnel_data = r.json()
            tunnel_id = tunnel_data.get("tunnel_id") or tunnel_data.get("session_id", "?")
            kv("tunnel_id", tunnel_id[:12] + "...")
            kv("target", f"http://127.0.0.1:{target_port}")

        # List active tunnels
        info("Fetching active tunnels...")
        r = await client.get("/api/tunnels")
        if r.status_code == 200:
            tunnels = r.json()
            if not isinstance(tunnels, list):
                tunnels = tunnels.get("tunnels", []) if isinstance(tunnels, dict) else []
        else:
            tunnels = []
        kv("tunnel count", len(tunnels))
        if tunnels:
            for k, v in list(tunnels[0].items())[:3]:
                kv(k, str(v)[:60])

        info("(Proxied HTTP traffic inspectable in browser — see browser recording)")
        ok("HTTP inspect tunnel active — requests/responses captured")

    stop_server(target_srv)
    stop_server(server)


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Record asciinema cast + browser screenshots for the HTTP inspect demo."""
    feat_dir = out_dir(FEATURE, base_out)

    # Record terminal demo with asciinema
    cast_path = asciinema_record(__file__, feat_dir / "terminal.cast")

    # Start target API and provide-uterm server
    target_port, target_srv = _start_target_api()
    base_url, server = start_server()
    time.sleep(1.0)

    # Attempt to create a tunnel for browser screenshots
    try:
        import httpx as _httpx

        with _httpx.Client(base_url=base_url, timeout=30.0) as client:
            client.post(
                "/api/tunnels",
                json={"tunnel_type": "http", "display_name": "demo-inspect", "local_port": target_port},
            )
    except Exception as exc:
        print(f"  [WARN] tunnel setup failed: {exc}", flush=True)

    # Pre-populate terminal with output before browser connects
    send_to_session(base_url, "provide-shell", "echo '--- http inspect tunnel ---'\r", wait_s=0.8)
    send_to_session(base_url, "provide-shell", f"curl -s http://127.0.0.1:{target_port}/health\r", wait_s=1.0)

    steps: list[BrowserStep] = [
        ("/app/session/provide-shell", 0.5, None),
        (lambda p: wait_for_terminal(p), 2.0, "01-session-terminal.png"),
        ("/api/tunnels", 1.0, "02-tunnels-list.png"),  # Shows active tunnels
        ("/app/", 1.0, "03-dashboard.png"),
    ]
    mp4_path = browser_record(base_url, steps, feat_dir)

    stop_server(target_srv)
    stop_server(server)

    highlight = trim_clip(mp4_path, HIGHLIGHT_START_S, HIGHLIGHT_DURATION_S)
    return {"cast": cast_path, "mp4": mp4_path, "highlight": highlight}


if __name__ == "__main__":
    if "--run-demo" in sys.argv:
        asyncio.run(run_terminal_demo())
    else:
        result = record()
        print(f"\nHTTP inspect demo: {result}")
