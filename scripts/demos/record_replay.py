#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: Record 10 seconds of terminal activity then scrub through replay in the browser."""

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

FEATURE = "replay"
DESCRIPTION = "Record 10 seconds of terminal activity then scrub through replay in the browser"
TITLE = "Session Replay"
SUBTITLE = "Scrub and replay recorded sessions"
HIGHLIGHT_START_S: float = 3.0
HIGHLIGHT_DURATION_S: float = 6.0


async def run_terminal_demo() -> None:
    """Run the replay feature demo."""
    base_url, server = start_server()
    await asyncio.sleep(1.5)

    banner(DESCRIPTION)

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0, headers=dev_bearer_headers()) as client:
        # Post 10 annotations to generate recording activity
        info("Posting 10 timeline annotations (step_00..step_09)...")
        for i in range(10):
            label = f"step_{i:02d}"
            r = await client.post(
                "/api/sessions/provide-shell/annotate",
                json={"label": label, "severity": "info"},
            )
            r.raise_for_status()
            seq = r.json().get("seq", "?")
            ok(f"{label} → seq={seq}")
            await asyncio.sleep(0.3)

        # Check recording status
        info("Checking recording status...")
        r = await client.get("/api/sessions/provide-shell/recording")
        r.raise_for_status()
        rec_meta = r.json()
        kv("format", rec_meta.get("format"))
        kv("enabled", rec_meta.get("enabled"))

        # Get recording entries count
        info("Fetching recording entries...")
        r = await client.get("/api/sessions/provide-shell/recording/entries")
        r.raise_for_status()
        entries = r.json()
        kv("total entries", len(entries))

        # Download the recording file
        info("Downloading JSONL recording file...")
        r = await client.get("/api/sessions/provide-shell/recording/download")
        kv("size", f"{len(r.content)} bytes")
        lines = r.text.strip().splitlines()
        kv("JSONL lines", len(lines))
        info("(Use browser replay player to scrub through entries)")

    ok("Recording ready for replay — 10 entries captured")
    stop_server(server)


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Record asciinema cast + browser screenshots for the replay demo."""
    feat_dir = out_dir(FEATURE, base_out)

    # Record terminal demo with asciinema
    cast_path = asciinema_record(__file__, feat_dir / "terminal.cast")

    # Start a fresh server for browser recording
    base_url, server = start_server()
    time.sleep(1.5)

    # Generate terminal activity and annotations so the replay player has content
    send_to_session(base_url, "provide-shell", "echo 'replay demo'\r", wait_s=0.8)
    send_to_session(base_url, "provide-shell", "uptime\r", wait_s=0.8)

    try:
        import httpx as _httpx

        with _httpx.Client(base_url=base_url, timeout=30.0, headers=dev_bearer_headers()) as client:
            for i in range(5):
                client.post(
                    "/api/sessions/provide-shell/annotate",
                    json={"label": f"step_{i:02d}", "severity": "info"},
                )
                time.sleep(0.2)
    except Exception as exc:
        print(f"  [WARN] annotation setup failed: {exc}", flush=True)

    steps: list[BrowserStep] = [
        ("/app/replay/provide-shell", 3.0, "01-replay-player.png"),  # wait 3s for SPA to mount
        (None, 2.0, "02-replay-scrubbing.png"),  # same page, second screenshot
        ("/app/session/provide-shell", 0.5, None),
        (lambda p: wait_for_terminal(p), 2.0, "03-live-session.png"),
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
        print(f"\nReplay demo: {result}")
