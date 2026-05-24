#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: 21 MCP tools for AI agent integration: session management, hijack, fan-out, annotation."""

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

FEATURE = "mcp"
DESCRIPTION = "21 MCP tools for AI agent integration: session management, hijack, fan-out, annotation"
TITLE = "MCP Integration"
SUBTITLE = "AI tools for terminal sessions"
HIGHLIGHT_START_S: float = 3.0
HIGHLIGHT_DURATION_S: float = 6.0


async def run_terminal_demo() -> None:
    """Run the MCP feature demo."""
    base_url, server = start_server()
    await asyncio.sleep(1.5)

    banner(DESCRIPTION)

    # Instantiate MCP app and list tools
    info("Initializing MCP server app...")
    from provide.uterm.ai.server import create_mcp_app

    mcp_app = create_mcp_app(base_url)
    tools = await mcp_app.list_tools()
    tool_names = sorted(t.name for t in tools)
    kv("tool count", len(tool_names))
    for name in tool_names[:8]:
        info(f"  • {name}")
    info(f"  ... and {len(tool_names) - 8} more")

    info("Demonstrating MCP tool calls via HTTP API:")

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0, headers=dev_bearer_headers()) as client:
        # list_sessions
        r = await client.get("/api/sessions")
        r.raise_for_status()
        sessions = r.json()
        ok(f"list_sessions → {len(sessions)} sessions")

        # get_snapshot
        r = await client.get("/api/sessions/provide-shell/snapshot")
        r.raise_for_status()
        snapshot = r.json() or {}
        ok(f"get_snapshot(provide-shell) → cols={snapshot.get('cols')} rows={snapshot.get('rows')}")

        # list_fanout_groups
        r = await client.get("/api/fanout/groups")
        r.raise_for_status()
        groups = r.json() if isinstance(r.json(), list) else r.json().get("groups", [])
        ok(f"list_fanout_groups → {len(groups)} groups")

        # annotate
        r = await client.post(
            "/api/sessions/provide-shell/annotate",
            json={"label": "mcp_demo", "description": "Called from MCP demo", "severity": "info"},
        )
        r.raise_for_status()
        seq = r.json().get("seq", "?")
        ok(f"annotate(provide-shell, mcp_demo) → seq={seq}")

        # get_recording_entries
        r = await client.get("/api/sessions/provide-shell/recording/entries")
        r.raise_for_status()
        entries = r.json()
        ok(f"get_recording_entries(provide-shell) → {len(entries)} entries")

    ok("21 MCP tools available — 5 demonstrated via HTTP API")
    stop_server(server)


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Record asciinema cast + browser screenshots for the MCP demo."""
    feat_dir = out_dir(FEATURE, base_out)

    # Record terminal demo with asciinema
    cast_path = asciinema_record(__file__, feat_dir / "terminal.cast")

    # Start a fresh server for browser recording
    base_url, server = start_server()
    time.sleep(1.5)

    # Pre-populate terminal with output before browser connects
    send_to_session(base_url, "provide-shell", "echo '--- MCP tools ready ---'\r", wait_s=0.8)
    send_to_session(base_url, "provide-shell", "echo 'AI agent connected'\r", wait_s=0.5)

    steps: list[BrowserStep] = [
        ("/app/session/provide-shell", 0.5, None),
        (lambda p: wait_for_terminal(p), 2.0, "01-session-mcp.png"),
        ("/api/health", 1.0, "02-api-health.png"),  # Shows server health with MCP info
        ("/api/sessions", 1.0, "03-sessions-list.png"),  # Shows sessions available to MCP
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
        print(f"\nMCP demo: {result}")
