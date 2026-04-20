#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: Enable session recording, produce terminal activity, download JSONL recording file."""

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

FEATURE = "recording"
DESCRIPTION = "Enable session recording, produce terminal activity, download JSONL recording file"
TITLE = "Session Recording"
SUBTITLE = "Record terminal sessions to file"
HIGHLIGHT_START_S: float = 3.0
HIGHLIGHT_DURATION_S: float = 6.0


async def run_terminal_demo() -> None:
    """Run the recording feature demo."""
    base_url, server = start_server()
    time.sleep(1.5)

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        banner(DESCRIPTION)

        # Check recording status
        info("Checking recording status...")
        r = await client.get("/api/sessions/provide-shell/recording")
        r.raise_for_status()
        rec = r.json()
        kv("enabled", rec.get("enabled"))
        kv("format", rec.get("format"))

        # Post annotations to generate recording activity
        info("Creating 5 recording entries via annotation...")
        for i in range(5):
            label = f"step_{i:02d}"
            r = await client.post(
                "/api/sessions/provide-shell/annotate",
                json={"label": label, "severity": "info"},
            )
            r.raise_for_status()
            seq = r.json().get("seq", "?")
            ok(f"{label} → seq={seq}")

        # Fetch recording entries
        info("Fetching recording entries...")
        r = await client.get("/api/sessions/provide-shell/recording/entries")
        r.raise_for_status()
        entries = r.json()
        kv("total entries", len(entries))
        for i, entry in enumerate(entries[:3]):
            info(f"  [{i}] {entry['event']} seq={entry.get('seq', '?')}")

        # Download the recording as JSONL
        info("Downloading recording as JSONL...")
        r = await client.get("/api/sessions/provide-shell/recording/download")
        r.raise_for_status()
        content = r.content
        lines = [ln for ln in content.splitlines() if ln.strip()]
        kv("byte size", len(content))
        kv("JSONL lines", len(lines))
        if lines:
            info(f"  sample: {lines[0][:80].decode(errors='replace')}")
        else:
            warn("no lines")

        ok("Recording enabled, entries stored, JSONL download complete")

    stop_server(server)


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Record asciinema cast + browser screenshots for the recording demo."""
    feat_dir = out_dir(FEATURE, base_out)

    # Record terminal demo with asciinema
    cast_path = asciinema_record(__file__, feat_dir / "terminal.cast")

    # Start a fresh server for browser recording
    base_url, server = start_server()
    time.sleep(1.5)

    # Pre-populate terminal with output before browser connects
    send_to_session(base_url, "provide-shell", "echo 'recording active'\r", wait_s=0.8)
    send_to_session(base_url, "provide-shell", "ls /tmp\r", wait_s=0.8)

    steps: list[BrowserStep] = [
        ("/app/session/provide-shell", 0.5, None),
        (lambda p: wait_for_terminal(p), 2.5, "01-session-recording.png"),
        ("/app/replay/provide-shell", 1.5, "02-replay-player.png"),
        ("/api/sessions/provide-shell/recording/entries", 1.0, "03-recording-entries.png"),
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
        print(f"\nRecording demo: {result}")
