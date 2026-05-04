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
    start_server,
    stop_server,
    trim_clip,
    wait_connected,
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

    # Use ushell connector so TermBridge connects to hub and responds to snapshot_req.
    # The shell connector is a simulated transcript and never sends snapshot messages,
    # so recording entries would have empty screen fields.
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

    # Wait for ushell to establish its WebSocket connection to the hub
    import httpx as _httpx

    wait_connected(base_url, "provide-shell", timeout=15.0)
    time.sleep(1.0)

    # Acquire a single hijack lease for the whole recording sequence
    hijack_id = ""
    try:
        with _httpx.Client(base_url=base_url, timeout=15.0) as http:
            http.patch("/api/sessions/provide-shell", json={"input_mode": "hijack"})
            r = http.post(
                "/worker/provide-shell/hijack/acquire",
                json={"owner": "demo-recorder", "lease_s": 120},
            )
            if r.status_code == 200:
                hijack_id = r.json().get("hijack_id", "")
    except Exception as exc:
        print(f"  [WARN] hijack setup: {exc}", flush=True)

    def _cmd(keys: str, wait_s: float = 0.7) -> None:
        """Send keys then call the snapshot endpoint to record the current screen state."""
        if not hijack_id:
            return
        try:
            with _httpx.Client(base_url=base_url, timeout=10.0) as http:
                http.post(f"/worker/provide-shell/hijack/{hijack_id}/send", json={"keys": keys})
                time.sleep(wait_s)
                # Calling this endpoint sends snapshot_req → worker responds with screen state
                # → hub records it as a "snapshot" event with the full screen content.
                http.get(f"/worker/provide-shell/hijack/{hijack_id}/snapshot")
        except Exception:
            pass

    # Run a sequence that tells a story — each command is a step in a deployment session.
    _cmd("clear\r", 0.5)
    _cmd("echo '=== provide-uterm: session recording active ==='\r")
    _cmd("date\r")
    _cmd("echo 'Recording all terminal I/O to JSONL...'\r")
    _cmd("ls -la /tmp | head -6\r", 0.9)
    _cmd("echo '[deploy] step 1: pulling config'\r")
    _cmd("echo '[deploy] step 2: running migrations'\r")
    _cmd("echo '[deploy] step 3: restarting services'\r")
    _cmd("echo '[deploy] healthcheck: ok — recording complete'\r")

    # Release hijack
    if hijack_id:
        try:
            with _httpx.Client(base_url=base_url, timeout=10.0) as http:
                http.post(f"/worker/provide-shell/hijack/{hijack_id}/release")
                http.patch("/api/sessions/provide-shell", json={"input_mode": "open"})
        except Exception:
            pass

    def _open_snapshot_filter(page: object) -> None:
        """Switch the replay filter to show only snapshot events.

        The select element doesn't include "snapshot" by default, so we inject
        the option and trigger a reload.  Each snapshot entry has a full screen
        field, making the replay visually meaningful (the rendered-screen pane
        changes at every step).
        """
        import contextlib

        with contextlib.suppress(Exception):
            page.evaluate(  # type: ignore[union-attr]
                """() => {
                    const f = document.querySelector('#replay-filter');
                    const lim = document.querySelector('#replay-limit');
                    if (!f || !lim) return;
                    // Inject 'snapshot' option and select it
                    const opt = document.createElement('option');
                    opt.value = 'snapshot'; opt.text = 'snapshot';
                    f.appendChild(opt);
                    f.value = 'snapshot';
                    // Set limit to 25 so the timeline shows our 9 command snapshots
                    lim.value = '25';
                    // A single change event on the filter triggers reload() with both values
                    f.dispatchEvent(new Event('change'));
                }"""
            )
        time.sleep(1.5)  # wait for filtered entries to load

    def _autoplay_replay(page: object) -> None:
        """Click Play and let replay animate through all snapshot entries."""
        import contextlib

        with contextlib.suppress(Exception):
            page.click("#btn-play")  # type: ignore[union-attr]
        # 9 snapshots * 800 ms/entry = 7.2 s; wait 10 s for full playback
        time.sleep(10.0)

    steps: list[BrowserStep] = [
        # Show live operator view so the terminal is visible and WebSocket connects
        ("/app/operator/provide-shell", 0.5, None),
        (lambda p: wait_for_terminal(p), 2.0, "01-session-active.png"),
        # Open replay viewer — shows step-by-step navigator
        ("/app/replay/provide-shell", 1.5, None),
        # Switch filter to snapshot-only (each entry has a full screen state)
        (_open_snapshot_filter, 0.0, "02-replay-loaded.png"),
        # Click Play and animate through all entries
        (_autoplay_replay, 0.0, "03-replay-playing.png"),
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
