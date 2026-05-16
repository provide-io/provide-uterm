#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""
Interactive terminal demo — multiple headed browsers, real telnet sessions.

Starts a provide-uterm FastAPI server with three sessions backed by real
telnet echo servers.  Opens five headed Playwright browser windows:
  - three watching telnet-alpha → proves SSE fanout
  - one watching shell-beta     → proves isolation
  - one watching telnet-gamma   → second independent session

Then types commands from the browser to prove end-to-end interactivity:
  - Input from browser 1 echoes back in all three alpha browsers.
  - beta browser input does NOT appear in any alpha browser.
  - Webhooks fire on every snapshot event from telnet-alpha.

Run:
    uv run python demo/run_demo.py
"""

from __future__ import annotations

import asyncio
import json
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from playwright.async_api import Page, async_playwright

# ---------------------------------------------------------------------------
# Path setup — allow importing from repo src trees
# ---------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[1]
for _p in [
    REPO / "packages/provide-uterm/src",
    REPO / "packages/provide-uterm/tests",
    REPO / "packages/provide-uterm/src",
]:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from provide.uterm.server.app import create_server_app  # noqa: E402
from provide.uterm.server.config import config_from_mapping  # noqa: E402
from provide.uterm.transports.telnet_server import (  # noqa: E402
    _build_telnet_handshake,  # type: ignore[attr-defined]
)

# ---------------------------------------------------------------------------
# Port constants — change here rather than inline
# ---------------------------------------------------------------------------
DEMO_SERVER_PORT = 8766
STATIC_SERVER_PORT = 9987
WEBHOOK_RECEIVER_PORT = 9988

# ---------------------------------------------------------------------------
# Telnet echo server
# ---------------------------------------------------------------------------


class _EchoHandler(socketserver.BaseRequestHandler):
    """Telnet echo server: handshake → welcome banner → echo everything back."""

    def handle(self) -> None:
        self.request.sendall(_build_telnet_handshake())
        label = self.server.session_label  # type: ignore[attr-defined]
        self.request.sendall(f"[{label}] TELNET ECHO READY\r\n".encode())
        while True:
            data = self.request.recv(4096)
            if not data:
                return
            self.request.sendall(data)


class _TelnetEchoServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], session_label: str) -> None:
        self.session_label = session_label
        super().__init__(addr, _EchoHandler)


# ---------------------------------------------------------------------------
# Static HTML file server
# ---------------------------------------------------------------------------

DEMO_DIR = Path(__file__).parent


class _StaticHandler(BaseHTTPRequestHandler):
    """Serves terminal.html at / and sse_viewer.html at /sse."""

    def log_message(self, *_: Any) -> None:
        pass  # silence access log

    def do_GET(self) -> None:
        fname = "sse_viewer.html" if self.path.startswith("/sse") else "terminal.html"
        data = (DEMO_DIR / fname).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------------------
# Demo helpers
# ---------------------------------------------------------------------------


async def _wait_for_worker(http: httpx.AsyncClient, session_id: str, timeout: float = 15.0) -> None:
    """Poll until the session's hosted runtime reports connected=True."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        with __import__("contextlib").suppress(Exception):
            resp = await http.get(f"/api/sessions/{session_id}")
            if resp.json().get("connected"):
                return
        await asyncio.sleep(0.3)
    raise RuntimeError(f"{session_id!r} worker did not come online within {timeout}s")


async def _type(page: Page, text: str) -> None:
    """Send text to the terminal session via window._demo_send()."""
    await page.evaluate("(data) => window._demo_send(data)", text)


# ---------------------------------------------------------------------------
# Demo driver
# ---------------------------------------------------------------------------


async def drive_demo(
    base_url: str,
    pages: list[Page],
    webhook_hits: list[dict[str, Any]],
) -> None:
    """Drive all demo scenarios after browsers are open and workers online."""
    page_a1, _page_a2, _page_a3, page_b = pages

    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as http:
        # Wait for telnet workers to finish connecting
        print("[demo] Waiting for telnet workers…")
        await asyncio.gather(
            _wait_for_worker(http, "telnet-alpha"),
            _wait_for_worker(http, "telnet-gamma"),
        )
        await asyncio.sleep(0.5)  # let browser WS connections settle

        # Register a webhook on telnet-alpha to prove delivery
        wh = (
            await http.post(
                "/api/sessions/telnet-alpha/webhooks",
                json={"url": f"http://127.0.0.1:{WEBHOOK_RECEIVER_PORT}/hook", "event_types": ["snapshot"]},
            )
        ).json()
        print(f"[demo]   webhook registered: {wh['webhook_id'][:8]}…")

        # ── Scenario 1: three browsers on telnet-alpha (fanout + interactivity) ──
        print("\n[demo] Scenario 1 — interactive input in browser 1, all 3 alpha browsers echo")
        for text in [
            "hello from the terminal\r",
            "provide-uterm SSE fanout works!\r",
            "echo three browsers see this\r",
        ]:
            await _type(page_a1, text)
            await asyncio.sleep(0.7)
            print(f"[demo]   typed: {text!r}")

        await asyncio.sleep(0.5)

        # ── Scenario 2: shell-beta isolation ─────────────────────────────────────
        print("\n[demo] Scenario 2 — shell-beta isolation (alpha browsers must stay silent)")
        await _type(page_b, "echo only shell-beta sees this\r")
        await asyncio.sleep(0.8)
        await _type(page_b, "echo isolation confirmed\r")
        await asyncio.sleep(0.5)
        print("[demo]   typed 2 commands into shell-beta")

        # ── Webhook summary ───────────────────────────────────────────────────────
        print(f"\n[demo] Webhook hits received: {len(webhook_hits)}")
        for hit in webhook_hits[:5]:
            ev = hit.get("event", {})
            sid = (hit.get("session_id") or "")[:12]
            print(f"[demo]   → type={ev.get('type')} session={sid}…")


# ---------------------------------------------------------------------------
# Mini webhook receiver
# ---------------------------------------------------------------------------


def _start_webhook_receiver(hits: list[dict[str, Any]]) -> None:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            with __import__("contextlib").suppress(Exception):
                hits.append(json.loads(body))
            self.send_response(200)
            self.end_headers()

    srv = HTTPServer(("127.0.0.1", WEBHOOK_RECEIVER_PORT), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    # Start webhook receiver
    webhook_hits: list[dict[str, Any]] = []
    _start_webhook_receiver(webhook_hits)

    # Start two telnet echo backends (random ports)
    telnet_a = _TelnetEchoServer(("127.0.0.1", 0), "alpha")
    telnet_b = _TelnetEchoServer(("127.0.0.1", 0), "gamma")
    for srv in (telnet_a, telnet_b):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    port_a = telnet_a.server_address[1]
    port_b = telnet_b.server_address[1]
    print(f"[demo] Telnet echo servers: alpha={port_a} gamma={port_b}")

    # Start static HTML server
    static_srv = HTTPServer(("127.0.0.1", STATIC_SERVER_PORT), _StaticHandler)
    threading.Thread(target=static_srv.serve_forever, daemon=True).start()
    print(f"[demo] Static HTML server: http://127.0.0.1:{STATIC_SERVER_PORT}/ (terminal.html)")

    # Build FastAPI server config — auto_start=True so connectors come up automatically
    cfg = config_from_mapping(
        {
            "server": {
                "host": "127.0.0.1",
                "port": DEMO_SERVER_PORT,
                "public_base_url": f"http://127.0.0.1:{DEMO_SERVER_PORT}",
                "allowed_origins": [f"http://127.0.0.1:{STATIC_SERVER_PORT}"],
            },
            "auth": {"mode": "dev"},
            "sessions": [
                {
                    "session_id": "telnet-alpha",
                    "display_name": "Telnet Alpha",
                    "connector_type": "telnet",
                    "auto_start": True,
                    "connector_config": {"host": "127.0.0.1", "port": port_a},
                },
                {
                    "session_id": "shell-beta",
                    "display_name": "Shell Beta",
                    "connector_type": "shell",
                    "auto_start": True,
                },
                {
                    "session_id": "telnet-gamma",
                    "display_name": "Telnet Gamma",
                    "connector_type": "telnet",
                    "auto_start": True,
                    "connector_config": {"host": "127.0.0.1", "port": port_b},
                },
            ],
        }
    )
    app = create_server_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=DEMO_SERVER_PORT, log_level="warning"))
    srv_task = asyncio.create_task(server.serve())

    # Wait for server ready
    deadline = asyncio.get_running_loop().time() + 10.0
    while not server.started:
        if asyncio.get_running_loop().time() > deadline:
            raise RuntimeError("uvicorn startup timeout")
        await asyncio.sleep(0.05)
    print(f"[demo] FastAPI server ready: http://127.0.0.1:{DEMO_SERVER_PORT}/")

    # Attach EventBus so SSE streams and webhooks work
    from provide.uterm.bridge.hub import EventBus

    app.state.uterm_registry._hub._event_bus = EventBus()

    # ── Open Playwright browsers ──────────────────────────────────────────────
    api = f"http://127.0.0.1:{DEMO_SERVER_PORT}"
    viewer = f"http://127.0.0.1:{STATIC_SERVER_PORT}"

    screenshots_dir = DEMO_DIR / "screenshots"
    screenshots_dir.mkdir(exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=60)

        def terminal_url(session: str, label: str) -> str:
            return f"{viewer}/?session={session}&base={api}&label={label}"

        ctx_a1 = await browser.new_context(viewport={"width": 900, "height": 600})
        ctx_a2 = await browser.new_context(viewport={"width": 900, "height": 600})
        ctx_a3 = await browser.new_context(viewport={"width": 900, "height": 600})
        ctx_b = await browser.new_context(viewport={"width": 900, "height": 600})

        page_a1 = await ctx_a1.new_page()
        page_a2 = await ctx_a2.new_page()
        page_a3 = await ctx_a3.new_page()
        page_b = await ctx_b.new_page()

        pages = [page_a1, page_a2, page_a3, page_b]
        labels = ["alpha-browser-1", "alpha-browser-2", "alpha-browser-3", "beta-browser"]

        # Navigate all four windows simultaneously
        await asyncio.gather(
            page_a1.goto(terminal_url("telnet-alpha", "telnet-alpha [browser 1]")),
            page_a2.goto(terminal_url("telnet-alpha", "telnet-alpha [browser 2]")),
            page_a3.goto(terminal_url("telnet-alpha", "telnet-alpha [browser 3]")),
            page_b.goto(terminal_url("shell-beta", "shell-beta  [browser 4]")),
        )
        await asyncio.sleep(2.0)  # let WS connections and auto-start workers settle
        print("[demo] Four browser windows open.")

        # Screenshot 1 — connected, showing initial terminal state
        for pg, lbl in zip(pages, labels, strict=True):
            await pg.screenshot(path=str(screenshots_dir / f"01_connected_{lbl}.png"))
        print("[demo] Screenshot 1: initial connection")

        # ── Drive the interactive demo ─────────────────────────────────────────
        await drive_demo(api, pages, webhook_hits)

        # Screenshot 2 — after interactive typing
        await asyncio.sleep(0.5)
        for pg, lbl in zip(pages, labels, strict=True):
            await pg.screenshot(path=str(screenshots_dir / f"02_interactive_{lbl}.png"))
        print("[demo] Screenshot 2: after interactive input")

        # Open a 5th window for telnet-gamma
        ctx_g = await browser.new_context(viewport={"width": 900, "height": 600})
        page_g = await ctx_g.new_page()
        await page_g.goto(terminal_url("telnet-gamma", "telnet-gamma [browser 5]"))
        await asyncio.sleep(1.5)
        await page_g.screenshot(path=str(screenshots_dir / "03_gamma_browser.png"))
        print("[demo] Screenshot 3: gamma session window")

        print(f"\n[demo] ✓ Screenshots saved to {screenshots_dir}/")
        print("[demo] Browsers open for 15 seconds — press Ctrl-C to quit early")
        await asyncio.sleep(15)

        await browser.close()

    server.should_exit = True
    await asyncio.wait_for(srv_task, timeout=5.0)
    print("[demo] Done.")


if __name__ == "__main__":
    asyncio.run(main())
