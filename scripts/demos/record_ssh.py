#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: Connect a session to an SSH host, run commands, show live output."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from typing import TYPE_CHECKING, Any

import asyncssh
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

FEATURE = "ssh"
DESCRIPTION = "Connect a session to an SSH host, run commands, show live output"
TITLE = "SSH Connector"
SUBTITLE = "Connect to remote hosts over SSH"
HIGHLIGHT_START_S: float = 3.0
HIGHLIGHT_DURATION_S: float = 6.0


async def _handle_shell(process: asyncssh.SSHServerProcess) -> None:  # type: ignore[type-arg]
    process.stdout.write("Welcome to Demo SSH Server\r\n$ ")
    await process.stdout.drain()
    # Keep the session alive for the demo before exiting
    await asyncio.sleep(10.0)
    process.exit(0)


class _DemoSSHServer(asyncssh.SSHServer):
    """SSH server that accepts any user without authentication."""

    def begin_auth(self, username: str) -> bool:  # type: ignore[override]  # noqa: ARG002
        # Return False to skip authentication
        return False


async def _start_ssh_server() -> tuple[asyncssh.SSHAcceptor, int]:
    port = free_port()
    server = await asyncssh.create_server(
        _DemoSSHServer,
        host="127.0.0.1",
        port=port,
        server_host_keys=[asyncssh.generate_private_key("ssh-rsa")],
        process_factory=_handle_shell,
    )
    return server, port


async def run_terminal_demo() -> None:
    """Run the SSH connector feature demo."""
    ssh_server, ssh_port = await _start_ssh_server()
    base_url, server = start_server()
    await asyncio.sleep(1.5)

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as http:
        banner(DESCRIPTION)

        info(f"Starting inline SSH server on 127.0.0.1:{ssh_port}...")
        ok("SSH server ready")

        # Create an SSH session
        info("Creating SSH session ssh-demo...")
        r = await http.post(
            "/api/sessions",
            json={
                "session_id": "ssh-demo",
                "display_name": "Demo SSH",
                "connector_type": "ssh",
                "auto_start": True,
                "connector_config": {
                    "host": "127.0.0.1",
                    "port": ssh_port,
                    "username": "demo",
                    "insecure_no_host_check": True,
                    "client_keys": [],
                },
            },
        )
        r.raise_for_status()
        data = r.json()
        kv("session_id", data.get("session_id"))
        kv("connector_type", data.get("connector_type"))
        kv("port", ssh_port)

        # Wait for connection (async poll so asyncssh event loop isn't blocked)
        info("Waiting for session to connect...")
        connected = False
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            r2 = await http.get("/api/sessions/ssh-demo")
            if r2.status_code == 200 and r2.json().get("connected"):
                connected = True
                break
            await asyncio.sleep(0.3)
        if connected:
            ok("ssh-demo connected")
        else:
            warn("timeout waiting for connection")

        await asyncio.sleep(1.0)

        # Take a snapshot of the session
        info("Fetching SSH session snapshot...")
        r = await http.get("/api/sessions/ssh-demo/snapshot")
        snapshot = r.json() or {}
        kv("cols", snapshot.get("cols"))
        kv("rows", snapshot.get("rows"))

        # Fetch session events
        info("Fetching session events...")
        r2 = await http.get("/api/sessions/ssh-demo/recording/entries")
        events = r2.json() if r2.status_code == 200 else []
        kv("event count", len(events))

        ok("SSH session established via asyncssh inline server")

    stop_server(server)
    ssh_server.close()


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Record asciinema cast + browser screenshots for the SSH demo."""
    feat_dir = out_dir(FEATURE, base_out)

    # Record terminal demo with asciinema
    cast_path = asciinema_record(__file__, feat_dir / "terminal.cast")

    # Start SSH server + provide-terminal server in a background thread
    loop_holder: list[asyncio.AbstractEventLoop] = []
    ssh_srv_holder: list[Any] = []
    base_url_holder: list[str] = []
    srv_holder: list[Any] = []

    def _run_setup() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop_holder.append(loop)

        async def _setup() -> None:
            ssh_srv, ssh_port = await _start_ssh_server()
            ssh_srv_holder.append(ssh_srv)
            base_url, srv = start_server()
            base_url_holder.append(base_url)
            srv_holder.append(srv)
            with httpx.Client(base_url=base_url, timeout=30.0) as http:
                http.post(
                    "/api/sessions",
                    json={
                        "session_id": "ssh-demo",
                        "display_name": "Demo SSH",
                        "connector_type": "ssh",
                        "auto_start": True,
                        "connector_config": {
                            "host": "127.0.0.1",
                            "port": ssh_port,
                            "username": "demo",
                            "insecure_no_host_check": True,
                            "client_keys": [],
                        },
                    },
                )
            wait_connected(base_url, "ssh-demo")

        loop.run_until_complete(_setup())

    t = threading.Thread(target=_run_setup)
    t.start()
    t.join()

    base_url = base_url_holder[0]
    time.sleep(0.5)

    # Send a command so the SSH terminal shows output before the browser connects
    send_to_session(base_url, "ssh-demo", "uptime\r", wait_s=1.0)

    steps: list[BrowserStep] = [
        ("/app/session/ssh-demo", 0.0, None),
        (lambda p: wait_for_terminal(p), 3.0, "01-ssh-terminal.png"),
        ("/app/operator/ssh-demo", 0.0, None),
        (lambda p: wait_for_terminal(p), 2.0, "02-ssh-operator.png"),
    ]
    mp4_path = browser_record(base_url, steps, feat_dir)

    stop_server(srv_holder[0])
    ssh_srv_holder[0].close()

    highlight = trim_clip(mp4_path, HIGHLIGHT_START_S, HIGHLIGHT_DURATION_S)
    return {"cast": cast_path, "mp4": mp4_path, "highlight": highlight}


if __name__ == "__main__":
    if "--run-demo" in sys.argv:
        asyncio.run(run_terminal_demo())
    else:
        result = record()
        print(f"\nSSH demo: {result}")
