#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: Multiple operator cursors join the same session, presence state is broadcast."""

from __future__ import annotations

import asyncio
import sys
import time
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from pathlib import Path

from provide.terminal.bridge.hub import TermHub
from provide.terminal.deckmux._hub_mixin import DeckMuxMixin
from scripts.demos import (
    BASE_OUT,
    BrowserStep,
    asciinema_record,
    banner,
    info,
    kv,
    ok,
    out_dir,
    record_simultaneous_perspectives,
    send_to_session,
    start_server,
    stop_server,
    trim_clip,
    wait_for_presence_bar,
    wait_for_terminal,
)


class _DeckMuxTermHub(DeckMuxMixin, TermHub):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._deckmux_init()


FEATURE = "deckmux"
DESCRIPTION = "Multiple operator cursors join the same session, presence state is broadcast"
TITLE = "DeckMux Presence"
SUBTITLE = "Multiple operators share a session"
HIGHLIGHT_START_S: float = 5.0
HIGHLIGHT_DURATION_S: float = 8.0


async def run_terminal_demo() -> None:
    """Run the DeckMux presence demo."""
    base_url, server = start_server(
        hub_class=_DeckMuxTermHub,
        sessions=[
            {
                "session_id": "provide-shell",
                "display_name": "Provide Shell",
                "connector_type": "pty",
                "input_mode": "open",
                "auto_start": True,
                "connector_config": {"command": "/bin/bash"},
            }
        ],
    )
    time.sleep(1.5)

    banner(DESCRIPTION)

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as http:
        # Confirm session is reachable
        info("Fetching session info...")
        r = await http.get("/api/sessions/provide-shell")
        r.raise_for_status()
        session = r.json()
        kv("session_id", session.get("session_id"))
        kv("connected", session.get("connected"))

        # Fetch the snapshot to confirm dimensions
        info("Fetching terminal snapshot...")
        r = await http.get("/api/sessions/provide-shell/snapshot")
        snapshot = r.json() or {}
        cols = snapshot.get("cols")
        rows = snapshot.get("rows")
        kv("cols", cols)
        kv("rows", rows)

        # Fetch recent events
        info("Fetching recent session events...")
        r = await http.get("/api/sessions/provide-shell/events")
        events = r.json() if r.status_code == 200 else []
        event_list = events if isinstance(events, list) else events.get("events", [])
        kv("event count", len(event_list))

    info("(Presence cursors are live via WebSocket — see browser recording)")
    ok("DeckMux presence enabled — multiple cursors share the session")
    stop_server(server)


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Record asciinema cast + two simultaneous browser videos for the DeckMux demo.

    Two operator browser contexts connect to the same session at the same time.
    user1.mp4 shows user1's view with user2's presence cursor visible.
    user2.mp4 shows user2's view with user1's presence cursor visible.
    Both videos prove multi-user presence is working — neither is a fake background.
    """
    base_url, server = start_server(
        hub_class=_DeckMuxTermHub,
        sessions=[
            {
                "session_id": "provide-shell",
                "display_name": "Provide Shell",
                "connector_type": "pty",
                "input_mode": "open",
                "auto_start": True,
                "connector_config": {"command": "/bin/bash"},
            }
        ],
    )
    time.sleep(1.5)

    feat_dir = out_dir(FEATURE, base_out)

    # Record terminal demo with asciinema
    cast_path = asciinema_record(__file__, feat_dir / "terminal.cast")
    # Pre-populate with real shell output so the terminal has visible content when browsers connect
    send_to_session(base_url, "provide-shell", "uptime\r", wait_s=0.5)
    send_to_session(base_url, "provide-shell", "echo '--- provide-terminal deckmux demo ---'\r", wait_s=0.3)
    send_to_session(base_url, "provide-shell", "ps aux | head -20\r", wait_s=1.0)

    # Both browser contexts open simultaneously — each sees the other's presence cursor.
    # Step 0: navigate (both open at once)
    # Step 1: wait for terminal then wait for presence bar with ≥2 avatars (both connected)
    # Step 2: short settle, then screenshot shows both user name avatars in the bar
    perspectives: dict[str, list[BrowserStep]] = {
        "user1": [
            ("/app/operator/provide-shell", 0.5, None),  # step 0: navigate
            (lambda p: wait_for_terminal(p), 0.5, None),  # step 1: terminal ready
            (
                lambda p: wait_for_presence_bar(p, min_users=2),
                1.0,
                "user1-presence-bar.png",
            ),  # step 2: bar with both
        ],
        "user2": [
            ("/app/operator/provide-shell", 0.5, None),  # step 0: navigate
            (lambda p: wait_for_terminal(p), 0.5, None),  # step 1: terminal ready
            (
                lambda p: wait_for_presence_bar(p, min_users=2),
                1.0,
                "user2-presence-bar.png",
            ),  # step 2: bar with both
        ],
    }
    vids = record_simultaneous_perspectives(perspectives, base_url, feat_dir)

    stop_server(server)
    highlight = trim_clip(vids.get("user1"), HIGHLIGHT_START_S, HIGHLIGHT_DURATION_S)
    return {
        "cast": cast_path,
        "user1_mp4": vids.get("user1"),
        "user2_mp4": vids.get("user2"),
        "highlight": highlight,
    }


if __name__ == "__main__":
    if "--run-demo" in sys.argv:
        asyncio.run(run_terminal_demo())
    else:
        result = record()
        print(f"\nDeckMux demo: {result}")
