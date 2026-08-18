#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: Agent self-annotation and automatic detection of 20 security/lifecycle patterns."""

from __future__ import annotations

import asyncio
import sys
import time
from typing import TYPE_CHECKING

import httpx2

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

FEATURE = "annotation"
DESCRIPTION = "Agent self-annotation and automatic detection of 20 security/lifecycle patterns"
TITLE = "Annotations"
SUBTITLE = "Label sessions with metadata"
HIGHLIGHT_START_S: float = 3.0
HIGHLIGHT_DURATION_S: float = 6.0


async def run_terminal_demo() -> None:
    """Run the annotation feature demo."""
    base_url, server = start_server()
    time.sleep(1.5)

    banner(DESCRIPTION)

    async with httpx2.AsyncClient(base_url=base_url, timeout=30.0, headers=dev_bearer_headers()) as client:
        # Post basic lifecycle annotations
        info("Posting lifecycle annotations...")
        for label, severity in [("demo_started", "info"), ("deploy_begin", "warning")]:
            r = await client.post(
                "/api/sessions/provide-shell/annotate",
                json={"label": label, "severity": severity},
            )
            r.raise_for_status()
            seq = r.json().get("seq", "?")
            ok(f"{label} (severity={severity}) seq={seq}")

        # Post pattern-triggering annotations
        info("Triggering security pattern detections...")
        pattern_annotations = [
            {
                "label": "pattern_sudo",
                "description": "sudo apt-get update",
                "severity": "warning",
            },
            {
                "label": "pattern_aws",
                "description": "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
                "severity": "critical",
            },
            {
                "label": "pattern_destructive",
                "description": "rm -rf /tmp/build",
                "severity": "critical",
            },
        ]
        for ann in pattern_annotations:
            r = await client.post("/api/sessions/provide-shell/annotate", json=ann)
            r.raise_for_status()
            description = ann.get("description", "")
            ok(f"{ann['label']}: '{description[:40]}'")

        # Fetch annotation recording entries
        info("Querying annotation entries...")
        r = await client.get("/api/sessions/provide-shell/recording/entries", params={"event": "annotation"})
        r.raise_for_status()
        entries = r.json()
        kv("total entries", len(entries))
        for i, entry in enumerate(entries[:3]):
            e = entry.get("data", entry)
            info(f"  [{i}] {e.get('label', '?')} — {e.get('severity', '?')}")

        ok("All annotations stored and queryable")

    stop_server(server)


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Record asciinema cast + browser screenshots for the annotation demo."""
    feat_dir = out_dir(FEATURE, base_out)

    # Record terminal demo with asciinema
    cast_path = asciinema_record(__file__, feat_dir / "terminal.cast")

    # Start a fresh server for browser recording
    base_url, server = start_server()
    time.sleep(1.5)

    # Pre-populate terminal with output so the browser shows live activity
    send_to_session(base_url, "provide-shell", "echo 'annotation demo'\r", wait_s=0.8)
    send_to_session(base_url, "provide-shell", "echo 'sudo apt-get update'\r", wait_s=0.8)

    steps: list[BrowserStep] = [
        ("/app/session/provide-shell", 0.5, None),
        (lambda p: wait_for_terminal(p), 2.0, "01-session-with-annotations.png"),
        ("/app/operator/provide-shell", 0.5, None),
        (lambda p: wait_for_terminal(p), 2.0, "02-operator-view.png"),
        # Stay on the operator page for the closing beat — the previous
        # version navigated to ``/api/sessions/.../recording/entries`` which
        # is the raw JSON API endpoint and rendered as a wall of unstyled
        # JSON in the captured mp4. The operator view already shows the
        # annotated session and is the right thing to linger on.
        (lambda p: wait_for_terminal(p), 1.5, "03-annotation-entries.png"),
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
        print(f"\nAnnotation demo: {result}")
