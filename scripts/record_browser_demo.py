#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Record a browser-based demo of provide-terminal via Playwright.

Produces:
  demo/recordings/browser-demo.webm  — browser video
  demo/recordings/screenshots/       — step-by-step screenshots

Usage:
    uv run python scripts/record_browser_demo.py
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from playwright.sync_api import sync_playwright
from provide.terminal.server import create_server_app, default_server_config

DEMO_DIR = Path("demo/recordings")
SCREENSHOTS_DIR = DEMO_DIR / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def _start_server() -> tuple[str, object]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    base_url = f"http://127.0.0.1:{port}"
    config = default_server_config()
    config.auth.mode = "dev"
    config.server.host = "127.0.0.1"
    config.server.port = port
    config.server.public_base_url = base_url
    config.recording.enabled_by_default = True
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


def _setup_demo_data(base_url: str) -> str:
    """Create sessions and fan-out group, return group_id."""
    with httpx.Client(base_url=base_url, timeout=30.0) as http:
        # Create fleet sessions
        for i in range(3):
            http.post(
                "/api/sessions",
                json={
                    "session_id": f"fleet-{i}",
                    "display_name": f"Fleet Node {i}",
                    "connector_type": "shell",
                    "auto_start": True,
                },
            )

        time.sleep(1.0)

        # Create fan-out group
        r = http.post(
            "/api/fanout/groups",
            json={
                "name": "demo-fleet",
                "worker_ids": ["fleet-0", "fleet-1", "fleet-2"],
            },
        )
        group_id = r.json()["group_id"]

        # Send a fan-out command
        http.post(
            f"/api/fanout/groups/{group_id}/send",
            json={
                "data": "help\r",
                "quiesce_ms": 1500,
                "max_response_ms": 5000,
            },
        )

        # Add annotation
        http.post(
            "/api/sessions/provide-shell/annotate",
            json={
                "label": "demo_started",
                "description": "Automated demo recording",
                "severity": "info",
            },
        )

        return group_id


def record_browser(base_url: str) -> None:
    """Record browser session with Playwright."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            record_video_dir=str(DEMO_DIR),
            record_video_size={"width": 1280, "height": 720},
        )
        page = context.new_page()

        # 1. Dashboard
        page.goto(f"{base_url}/app/")
        page.wait_for_load_state("networkidle")
        time.sleep(1.0)
        page.screenshot(path=str(SCREENSHOTS_DIR / "01-dashboard.png"))
        print("  📸 Dashboard")

        # 2. Session page
        page.goto(f"{base_url}/app/session/provide-shell")
        page.wait_for_load_state("networkidle")
        time.sleep(1.5)
        page.screenshot(path=str(SCREENSHOTS_DIR / "02-session.png"))
        print("  📸 Session view")

        # 3. Operator page
        page.goto(f"{base_url}/app/operator/provide-shell")
        page.wait_for_load_state("networkidle")
        time.sleep(1.5)
        page.screenshot(path=str(SCREENSHOTS_DIR / "03-operator.png"))
        print("  📸 Operator view")

        # 4. Replay page
        page.goto(f"{base_url}/app/replay/provide-shell")
        page.wait_for_load_state("networkidle")
        time.sleep(1.5)
        page.screenshot(path=str(SCREENSHOTS_DIR / "04-replay.png"))
        print("  📸 Replay view")

        # 5. Fleet sessions
        page.goto(f"{base_url}/app/session/fleet-0")
        page.wait_for_load_state("networkidle")
        time.sleep(1.0)
        page.screenshot(path=str(SCREENSHOTS_DIR / "05-fleet-0.png"))
        print("  📸 Fleet node 0")

        # 6. API health
        page.goto(f"{base_url}/api/health")
        time.sleep(0.5)
        page.screenshot(path=str(SCREENSHOTS_DIR / "06-api-health.png"))
        print("  📸 API health")

        # 7. Fan-out groups
        page.goto(f"{base_url}/api/fanout/groups")
        time.sleep(0.5)
        page.screenshot(path=str(SCREENSHOTS_DIR / "07-fanout-groups.png"))
        print("  📸 Fan-out groups")

        # 8. Annotations in recording
        page.goto(f"{base_url}/api/sessions/provide-shell/recording/entries?event=annotation")
        time.sleep(0.5)
        page.screenshot(path=str(SCREENSHOTS_DIR / "08-annotations.png"))
        print("  📸 Annotations")

        context.close()
        browser.close()

        # Rename video
        videos = list(DEMO_DIR.glob("*.webm"))
        if videos:
            latest = max(videos, key=lambda p: p.stat().st_mtime)
            target = DEMO_DIR / "browser-demo.webm"
            latest.rename(target)
            print(f"\n  🎬 Video saved to {target}")

            # Convert to mp4
            import subprocess  # nosec

            mp4_path = DEMO_DIR / "browser-demo.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(target), "-c:v", "libx264", "-preset", "fast", str(mp4_path)],
                capture_output=True,
                timeout=60,
            )
            if mp4_path.exists():
                print(f"  🎬 MP4 saved to {mp4_path}")


def main() -> None:
    print("\n\033[1;35m=== Browser Demo Recording ===\033[0m\n")

    base_url, server = _start_server()
    print(f"  Server: {base_url}")

    group_id = _setup_demo_data(base_url)
    print(f"  Demo data created (group: {group_id[:12]}...)\n")

    record_browser(base_url)

    # Create GIF from screenshots
    import subprocess  # nosec

    gif_path = DEMO_DIR / "demo.gif"
    pngs = sorted(SCREENSHOTS_DIR.glob("*.png"))
    if pngs and len(pngs) >= 3:
        # Use ffmpeg to create GIF from screenshots
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-framerate",
                "1",
                "-pattern_type",
                "glob",
                "-i",
                str(SCREENSHOTS_DIR / "*.png"),
                "-vf",
                "scale=1280:-1",
                str(gif_path),
            ],
            capture_output=True,
            timeout=60,
        )
        if gif_path.exists():
            print(f"  🎞️  GIF saved to {gif_path}")

    server.should_exit = True
    print("\n\033[1;35m=== Recording Complete ===\033[0m\n")
    print(f"  Files in {DEMO_DIR}/:")
    for f in sorted(DEMO_DIR.iterdir()):
        if f.is_file():
            size = f.stat().st_size
            print(f"    {f.name:30s} {size:>10,} bytes")


if __name__ == "__main__":
    main()
