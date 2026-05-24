#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: Viewer connects read-only, operator takes exclusive control, admin force-reclaims."""

from __future__ import annotations

import asyncio
import sys
import time
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

from scripts.demos import (
    BASE_OUT,
    BrowserStep,
    asciinema_record,
    banner,
    click_hijack,
    dev_bearer_headers,
    info,
    kv,
    ok,
    out_dir,
    record_simultaneous_perspectives,
    send_to_session,
    start_server,
    stop_server,
    trim_clip,
    type_in_terminal,
    wait_for_status,
    wait_for_terminal,
)

FEATURE = "hijack"
DESCRIPTION = "Viewer connects read-only, operator takes exclusive control, admin force-reclaims"
TITLE = "Session Hijack"
SUBTITLE = "Operator takes control, viewer watches"
HIGHLIGHT_START_S: float = 6.0
HIGHLIGHT_DURATION_S: float = 8.0
# Multi-browser demo: this recorder produces operator-side and viewer-side
# videos. The operator clip is the canonical highlight for the site catalog.
PRIMARY_VIDEO: str = "operator_trim.mp4"


async def run_terminal_demo() -> None:
    """Run the hijack lifecycle demo."""
    base_url, server = start_server()
    time.sleep(1.5)

    banner(DESCRIPTION)

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0, headers=dev_bearer_headers()) as http:
        # Switch provide-shell to hijack input mode (required for REST hijack)
        info("Switching provide-shell to hijack input mode...")
        r = await http.patch("/api/sessions/provide-shell", json={"input_mode": "hijack"})
        r.raise_for_status()
        ok("mode=hijack applied")
        time.sleep(0.5)

        # Viewer acquires a read-only hijack lease
        info("Viewer acquires read-only lease...")
        r = await http.post(
            "/worker/provide-shell/hijack/acquire",
            json={"owner": "viewer", "lease_s": 60},
        )
        r.raise_for_status()
        data = r.json()
        hijack_id_viewer = data.get("hijack_id", "")
        ok(f"hijack_id={hijack_id_viewer[:12]}...")
        kv("owner", "viewer")

        # Viewer fetches a snapshot
        info("Viewer fetches snapshot...")
        r = await http.get(f"/worker/provide-shell/hijack/{hijack_id_viewer}/snapshot")
        r.raise_for_status()
        snapshot = r.json()
        kv("cols", snapshot.get("cols"))
        kv("rows", snapshot.get("rows"))

        # Viewer releases so operator can take over
        info("Viewer releases — operator takes over...")
        r = await http.post(f"/worker/provide-shell/hijack/{hijack_id_viewer}/release")
        r.raise_for_status()
        ok("viewer released")

        # Operator acquires exclusive control
        info("Operator acquires exclusive control...")
        r = await http.post(
            "/worker/provide-shell/hijack/acquire",
            json={"owner": "operator", "lease_s": 60},
        )
        r.raise_for_status()
        data = r.json()
        hijack_id_op = data.get("hijack_id", "")
        ok(f"hijack_id={hijack_id_op[:12]}...")
        kv("owner", "operator")

        # Operator sends a command
        info("Operator sends command: uptime\\r")
        r = await http.post(
            f"/worker/provide-shell/hijack/{hijack_id_op}/send",
            json={"keys": "uptime\r"},
        )
        r.raise_for_status()
        ok("command delivered")

        # Operator releases
        r = await http.post(f"/worker/provide-shell/hijack/{hijack_id_op}/release")
        r.raise_for_status()

        # Admin force-reclaims with a new lease
        info("Admin force-reclaims session...")
        r = await http.post(
            "/worker/provide-shell/hijack/acquire",
            json={"owner": "admin", "lease_s": 30},
        )
        r.raise_for_status()
        data = r.json()
        hijack_id_admin = data.get("hijack_id", "")

        # Admin releases the lease
        r = await http.post(f"/worker/provide-shell/hijack/{hijack_id_admin}/release")
        r.raise_for_status()
        ok("hijack force-released")

        ok("Full viewer → operator → admin lifecycle complete")

    stop_server(server)


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Record asciinema cast + two simultaneous browser videos (viewer and operator).

    Viewer and operator are live at the same time.  The operator acquires the hijack
    lease and types a command while the viewer is connected — the viewer's final
    screenshot shows the output appearing in the terminal, proving live propagation.
    """
    feat_dir = out_dir(FEATURE, base_out)

    base_url, server = start_server()
    time.sleep(1.5)

    # Pre-populate terminal, then switch to hijack mode
    send_to_session(base_url, "provide-shell", "echo 'session ready for hijack'\r", wait_s=0.8)
    send_to_session(base_url, "provide-shell", "uptime\r", wait_s=0.8)
    with httpx.Client(base_url=base_url, timeout=30.0, headers=dev_bearer_headers()) as http:
        http.patch("/api/sessions/provide-shell", json={"input_mode": "hijack"})

    cast_path = asciinema_record(__file__, feat_dir / "terminal.cast")

    def operator_actions(page: Page) -> None:
        wait_for_terminal(page)
        click_hijack(page)
        wait_for_status(page, "Hijacked")
        time.sleep(0.5)
        type_in_terminal(page, "echo hijacked\\r")
        time.sleep(1.5)

    # Interleaved steps: both contexts stay open the entire time.
    # Viewer's step 3 runs after operator's step 2 (the typing) — so viewer sees output.
    perspectives: dict[str, list[BrowserStep]] = {
        "viewer": [
            ("/app/session/provide-shell", 0.5, None),  # step 0: navigate
            (lambda p: wait_for_terminal(p), 1.0, "viewer-01-initial.png"),  # step 1: initial state
            (None, 0.0, None),  # step 2: idle while operator acts
            (None, 2.0, "viewer-02-sees-operator-output.png"),  # step 3: operator output visible
        ],
        "operator": [
            ("/app/operator/provide-shell", 0.5, None),  # step 0: navigate
            (lambda p: wait_for_terminal(p), 1.0, "operator-01-initial.png"),  # step 1: initial state
            (operator_actions, 1.5, "operator-02-hijacked-and-typed.png"),  # step 2: take control + type
            (None, 0.5, "operator-03-result.png"),  # step 3: final state
        ],
    }
    vids = record_simultaneous_perspectives(perspectives, base_url, feat_dir)

    stop_server(server)
    highlight = trim_clip(vids.get("operator"), HIGHLIGHT_START_S, HIGHLIGHT_DURATION_S)
    return {
        "cast": cast_path,
        "viewer_mp4": vids.get("viewer"),
        "operator_mp4": vids.get("operator"),
        "highlight": highlight,
    }


if __name__ == "__main__":
    if "--run-demo" in sys.argv:
        asyncio.run(run_terminal_demo())
    else:
        result = record()
        print(f"\nHijack demo: {result}")
