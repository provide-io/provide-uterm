#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: Connect a session to a local telnet server, show negotiation and live output."""

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
    wait_connected,
    wait_for_terminal,
    warn,
)

FEATURE = "telnet"
DESCRIPTION = "Connect a session to a local telnet server, show negotiation and live output"
TITLE = "Telnet Connector"
SUBTITLE = "Legacy telnet session management"
HIGHLIGHT_START_S: float = 3.0
HIGHLIGHT_DURATION_S: float = 6.0
SITE_FORMAT = "mp4"


async def _telnet_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Simple demo telnet handler that echoes input."""
    writer.write(b"Welcome to Demo Telnet Server\r\n> ")
    await writer.drain()
    try:
        while True:
            data = await asyncio.wait_for(reader.read(256), timeout=5.0)
            if not data:
                break
            writer.write(data + b"\r\n> ")
            await writer.drain()
    except (TimeoutError, ConnectionResetError):
        pass
    writer.close()


async def _start_telnet(port: int) -> asyncio.Server:
    """Start a demo telnet server on the given port."""
    from provide.uterm.transports.telnet_server import start_telnet_server

    return await start_telnet_server(_telnet_handler, host="127.0.0.1", port=port)


async def run_terminal_demo() -> None:
    """Run the telnet connector feature demo."""
    telnet_port = free_port()
    telnet_server = await _start_telnet(telnet_port)
    base_url, server = start_server()
    await asyncio.sleep(1.5)

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        banner(DESCRIPTION)

        info(f"Starting inline telnet server on 127.0.0.1:{telnet_port}...")
        ok("Telnet server ready")

        # Create a telnet session
        info("Creating telnet session telnet-demo...")
        r = await client.post(
            "/api/sessions",
            json={
                "session_id": "telnet-demo",
                "connector_type": "telnet",
                "auto_start": True,
                "connector_config": {"host": "127.0.0.1", "port": telnet_port},
            },
        )
        r.raise_for_status()
        data = r.json()
        kv("session_id", data.get("session_id"))
        kv("connector_type", data.get("connector_type"))
        kv("port", telnet_port)

        # Wait for connection
        info("Waiting for session to connect...")
        connected = wait_connected(base_url, "telnet-demo")
        if connected:
            ok("telnet-demo connected")
        else:
            warn("timeout waiting for connection")

        await asyncio.sleep(1.0)

        # Take a snapshot of the session
        info("Fetching snapshot...")
        r = await client.get("/api/sessions/telnet-demo/snapshot")
        snapshot = r.json() or {}
        kv("cols", snapshot.get("cols"))
        kv("rows", snapshot.get("rows"))

        # Fetch session events
        info("Fetching session events...")
        r2 = await client.get("/api/sessions/telnet-demo/recording/entries")
        events = r2.json() if r2.status_code == 200 else []
        kv("event count", len(events))

        ok("Telnet session established via inline asyncio server")

    telnet_server.close()
    await telnet_server.wait_closed()
    stop_server(server)


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Record asciinema cast + browser screenshots for the telnet demo."""
    feat_dir = out_dir(FEATURE, base_out)

    # Record terminal demo with asciinema
    cast_path = asciinema_record(__file__, feat_dir / "terminal.cast")

    # Start telnet + provide-uterm server in a background thread
    # (async setup needs its own event loop so it doesn't conflict with main thread)
    telnet_port = free_port()
    holders: dict[str, Any] = {}

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _setup() -> None:
            tel_srv = await _start_telnet(telnet_port)
            holders["tel"] = tel_srv
            base_url, srv = start_server()
            holders["base_url"] = base_url
            holders["srv"] = srv
            with httpx.Client(base_url=base_url, timeout=30.0) as http:
                http.post(
                    "/api/sessions",
                    json={
                        "session_id": "telnet-demo",
                        "connector_type": "telnet",
                        "auto_start": True,
                        "connector_config": {"host": "127.0.0.1", "port": telnet_port},
                    },
                )
            wait_connected(base_url, "telnet-demo")

        loop.run_until_complete(_setup())

    t = threading.Thread(target=_run)
    t.start()
    t.join()

    base_url = holders["base_url"]
    time.sleep(0.5)

    # Send a command so the telnet terminal shows output before the browser connects
    send_to_session(base_url, "telnet-demo", "uptime\r", wait_s=1.0)

    steps: list[BrowserStep] = [
        ("/app/session/telnet-demo", 0.0, None),
        (lambda p: wait_for_terminal(p), 3.0, "01-telnet-terminal.png"),
        ("/app/operator/telnet-demo", 0.0, None),
        (lambda p: wait_for_terminal(p), 2.0, "02-telnet-operator.png"),
    ]
    mp4_path = browser_record(base_url, steps, feat_dir)

    stop_server(holders["srv"])
    tel_srv = holders.get("tel")
    if tel_srv is not None:
        tel_srv.close()

    highlight = trim_clip(mp4_path, HIGHLIGHT_START_S, HIGHLIGHT_DURATION_S)
    return {"cast": cast_path, "mp4": mp4_path, "highlight": highlight}


if __name__ == "__main__":
    if "--run-demo" in sys.argv:
        asyncio.run(run_terminal_demo())
    else:
        result = record()
        print(f"\nTelnet demo: {result}")
