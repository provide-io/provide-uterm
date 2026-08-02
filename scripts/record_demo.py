#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Record a demo of provide-uterm features: fan-out, annotation, shell.

Produces:
  demo/recordings/demo.cast    — asciinema recording
  demo/recordings/demo.mp4     — screen recording (via playwright)
  demo/recordings/demo.gif     — animated GIF (from playwright screenshots)

Usage:
    uv run python scripts/record_demo.py
"""

from __future__ import annotations

import asyncio
import socket
import subprocess  # nosec
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from provide.uterm.server import create_server_app, default_server_config

DEMO_DIR = Path("demo/recordings")
DEMO_DIR.mkdir(parents=True, exist_ok=True)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server() -> tuple[str, Any]:
    """Start a demo server and return (base_url, server)."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    config = default_server_config()
    config.auth.mode = "dev"
    config.server.host = "127.0.0.1"
    config.server.port = port
    config.server.public_base_url = base_url
    config.recording.enabled_by_default = True
    config.recording.directory = DEMO_DIR / "session_logs"
    app = create_server_app(config)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("Server did not start")
        time.sleep(0.05)
    return base_url, server


async def demo_fanout(http: httpx.AsyncClient) -> None:
    """Demonstrate fan-out: create sessions, group, broadcast."""
    print("\n\033[1;36m=== Fan-Out Demo ===\033[0m\n")

    # Create 3 shell sessions
    for i in range(3):
        r = await http.post(
            "/api/sessions",
            json={
                "session_id": f"fleet-{i}",
                "display_name": f"Fleet Node {i}",
                "connector_type": "shell",
                "auto_start": True,
            },
        )
        print(f"  Created session fleet-{i}: {r.status_code}")

    await asyncio.sleep(1.0)  # let sessions start

    # Create fan-out group
    r = await http.post(
        "/api/fanout/groups",
        json={
            "name": "demo-fleet",
            "worker_ids": ["fleet-0", "fleet-1", "fleet-2"],
            "mode": "parallel",
        },
    )
    group_id = r.json()["group_id"]
    print(f"  Created fan-out group: {group_id[:12]}...")

    # Broadcast a command
    r = await http.post(
        f"/api/fanout/groups/{group_id}/send",
        json={
            "data": "help\r",
            "quiesce_ms": 1500,
            "max_response_ms": 5000,
        },
    )
    result = r.json()
    print("  Broadcast 'help' to 3 sessions:")
    for sr in result["results"]:
        ok = "\033[32m✓\033[0m" if sr["ok"] else "\033[31m✗\033[0m"
        delta_len = len(sr.get("output_delta") or "")
        print(f"    {ok} {sr['worker_id']}: {delta_len} bytes output")
    print(f"  Divergent sessions: {result['divergent_sessions']}")
    print(f"  Failed sessions: {result['failed_sessions']}")


async def demo_annotation(http: httpx.AsyncClient) -> None:
    """Demonstrate annotation: agent self-annotation + pattern detection."""
    print("\n\033[1;36m=== Annotation Demo ===\033[0m\n")

    # Agent self-annotation
    r = await http.post(
        "/api/sessions/provide-shell/annotate",
        json={
            "label": "demo_started",
            "description": "Provide-uterm demo recording in progress",
            "severity": "info",
        },
    )
    print(f"  Agent annotation: {r.status_code} (seq={r.json().get('seq', '?')})")

    # Query annotations
    r = await http.get("/api/sessions/provide-shell/recording/entries", params={"event": "annotation"})
    entries = r.json()
    print(f"  Recording has {len(entries)} annotation(s)")
    for e in entries[-3:]:
        data = e.get("data", {})
        print(f"    [{data.get('severity', '?')}] {data.get('label', '?')}: {data.get('description', '')[:60]}")


async def demo_session_api(http: httpx.AsyncClient) -> None:
    """Demonstrate core session API."""
    print("\n\033[1;36m=== Session API Demo ===\033[0m\n")

    # Health check
    r = await http.get("/api/health")
    print(f"  Health: {r.json()['ok']}")

    # List sessions
    r = await http.get("/api/sessions")
    sessions = r.json()
    print(f"  Sessions: {len(sessions)}")
    for s in sessions[:5]:
        connected = "\033[32m●\033[0m" if s.get("connected") else "\033[31m○\033[0m"
        print(f"    {connected} {s['session_id']} ({s['connector_type']})")

    # Metrics
    r = await http.get("/api/metrics")
    metrics = r.json()["metrics"]
    print(f"  Metrics: {len(metrics)} counters")


async def run_demo() -> None:
    """Run the full demo."""
    print("\033[1;35m╔══════════════════════════════════════════╗\033[0m")
    print("\033[1;35m║   provide-uterm v0.5.0 Feature Demo   ║\033[0m")
    print("\033[1;35m╚══════════════════════════════════════════╝\033[0m")

    base_url, server = _start_server()
    print(f"\n  Server running at {base_url}\n")

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as http:
        await demo_session_api(http)
        await demo_fanout(http)
        await demo_annotation(http)

    print("\n\033[1;35m╔══════════════════════════════════════════╗\033[0m")
    print("\033[1;35m║              Demo Complete                ║\033[0m")
    print("\033[1;35m╚══════════════════════════════════════════╝\033[0m\n")

    server.should_exit = True


def record_asciinema() -> Path:
    """Record the demo as an asciinema cast file."""
    cast_path = DEMO_DIR / "demo.cast"
    print(f"Recording asciinema to {cast_path}...")
    subprocess.run(
        [
            "asciinema",
            "rec",
            str(cast_path),
            "--overwrite",
            "-c",
            f"{sys.executable} scripts/record_demo.py --run-demo",
        ],
        check=True,
        timeout=120,
    )
    return cast_path


def record_playwright() -> Path:
    """Record browser demo as MP4 via Playwright."""
    video_path = DEMO_DIR / "demo.mp4"
    # This would require a separate playwright script that opens the browser UI
    # For now, create a placeholder
    print(f"Playwright recording: {video_path} (requires browser UI)")
    return video_path


if __name__ == "__main__":
    if "--run-demo" in sys.argv:
        asyncio.run(run_demo())
    else:
        # Record asciinema
        record_asciinema()
        print(f"\nRecordings saved to {DEMO_DIR}/")
