#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: Spawn a local PTY session, run commands, show resize and snapshot."""

from __future__ import annotations

import asyncio
import sys
import time
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
    dev_bearer_headers,
    info,
    kv,
    ok,
    out_dir,
    send_to_session,
    start_server,
    stop_server,
    trim_clip,
    wait_for_terminal,
)

FEATURE = "pty"
DESCRIPTION = "Spawn a local PTY session, run commands, show resize and snapshot"
TITLE = "PTY Sessions"
SUBTITLE = "Live terminal over a local PTY"
HIGHLIGHT_START_S: float = 3.0
HIGHLIGHT_DURATION_S: float = 6.0
SITE_FORMAT = "mp4"


async def run_terminal_demo() -> None:
    """Run the PTY feature demo."""
    base_url, server = start_server()
    time.sleep(1.5)

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0, headers=dev_bearer_headers()) as client:
        banner(DESCRIPTION)

        # Get the default provide-shell session info
        info("Fetching PTY session info...")
        r = await client.get("/api/sessions/provide-shell")
        r.raise_for_status()
        session = r.json()
        kv("session_id", session.get("session_id"))
        kv("connector_type", session.get("connector_type"))
        kv("connected", session.get("connected"))

        # Get the session snapshot (cols, rows, cursor)
        info("Fetching terminal snapshot...")
        r = await client.get("/api/sessions/provide-shell/snapshot")
        r.raise_for_status()
        snapshot = r.json() or {}
        kv("cols", snapshot.get("cols"))
        kv("rows", snapshot.get("rows"))
        cursor = snapshot.get("cursor") or {}
        kv("cursor", f"x={cursor.get('x', 0)} y={cursor.get('y', 0)}")

        # Get metrics
        info("Fetching session metrics...")
        r = await client.get("/api/metrics")
        r.raise_for_status()
        metrics_data = r.json()
        all_metrics = metrics_data.get("metrics", {})
        relevant = {k: v for k, v in all_metrics.items() if "session" in k.lower() or "byte" in k.lower()}
        for k, v in list(relevant.items())[:3]:
            kv(k, v)

        ok("PTY session live — cols/rows/cursor confirmed")

    stop_server(server)


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Record asciinema cast + browser screenshots for the PTY demo."""
    feat_dir = out_dir(FEATURE, base_out)

    # Record terminal demo with asciinema
    cast_path = asciinema_record(__file__, feat_dir / "terminal.cast")

    # Start a fresh server for browser recording
    base_url, server = start_server()
    time.sleep(1.5)

    # Pre-populate the terminal with command output before the browser connects
    send_to_session(base_url, "provide-shell", "uname -a\r", wait_s=1.0)
    send_to_session(base_url, "provide-shell", "uptime\r", wait_s=1.0)
    send_to_session(base_url, "provide-shell", "echo '--- PTY demo ---'\r", wait_s=0.5)

    steps: list[BrowserStep] = [
        ("/app/session/provide-shell", 0.0, None),
        (lambda p: wait_for_terminal(p), 2.5, "01-pty-session.png"),
        ("/app/operator/provide-shell", 0.0, None),
        (lambda p: wait_for_terminal(p), 2.0, "02-pty-operator.png"),
    ]
    mp4_path = browser_record(base_url, steps, feat_dir)
    stop_server(server)

    highlight = trim_clip(mp4_path, HIGHLIGHT_START_S, HIGHLIGHT_DURATION_S)
    return {"cast": cast_path, "mp4": mp4_path, "highlight": highlight}


if __name__ == "__main__":
    if "--run-demo" in sys.argv:
        asyncio.run(run_terminal_demo())
    else:
        result = record()
        print(f"\nPTY demo: {result}")
