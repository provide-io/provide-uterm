#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Standalone E2E proof for Buffered Input State Machine.
Run with: uv run python packages/provide-uterm-server/tests/e2e/test_approval_buffered_input_e2e.py
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import httpx2
import uvicorn
from playwright.sync_api import sync_playwright

from provide.uterm.control_channel import ControlFrameDecoder, DataChunk, encode_control_frame
from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.hub.ext import PolicyDecision, PolicyGate


class ProofGate(PolicyGate):
    def __init__(self):
        self.calls = 0

    async def intercept_input(self, ctx, data):
        self.calls += 1
        if self.calls == 1:
            return PolicyDecision(action="hold", timeout_s=60, request_id="proof-req")
        return PolicyDecision(action="allow")


def run_proof():
    worker_id = "proof-worker"
    port = 54321
    base_url = f"http://127.0.0.1:{port}"

    captured_hub: TermHub | None = None

    class CapturingHub(TermHub):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            nonlocal captured_hub
            super().__init__(*args, **kwargs)
            captured_hub = self

    # 1. Start Server
    config = default_server_config()
    config.server.host = "127.0.0.1"
    config.server.port = port
    config.server.public_base_url = base_url
    config.sessions = []

    app = create_server_app(config, hub_class=CapturingHub)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))

    server_loop = asyncio.new_event_loop()

    def _run_server():
        asyncio.set_event_loop(server_loop)
        server_loop.run_until_complete(server.serve())

    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()

    while not server.started:
        time.sleep(0.1)

    print(f"Server started at {base_url}")

    # 2. Start Mock Worker
    worker_msgs: list[str] = []
    worker_ready = threading.Event()
    worker_stop = threading.Event()

    def _run_worker():
        from provide.uterm.client import connect_async_ws

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _worker_logic():
            ws_url = base_url.replace("http://", "ws://") + f"/ws/worker/{worker_id}/term"
            decoder = ControlFrameDecoder()
            async with connect_async_ws(ws_url) as worker_ws:
                await asyncio.wait_for(worker_ws.recv(), timeout=5.0)

                await worker_ws.send(
                    encode_control_frame(
                        {
                            "type": "snapshot",
                            "screen": "Welcome to proof terminal\r\n$",
                            "cursor": {"x": 1, "y": 1},
                            "ts": time.time(),
                        }
                    )
                )

                worker_ready.set()
                while not worker_stop.is_set():
                    try:
                        raw = await asyncio.wait_for(worker_ws.recv(), timeout=0.5)
                        for ev in decoder.feed(raw):
                            if isinstance(ev, DataChunk):
                                print(f"WORKER RECV: {ev.data!r}")
                                worker_msgs.append(ev.data)
                    except TimeoutError:
                        continue

        loop.run_until_complete(_worker_logic())

    worker_thread = threading.Thread(target=_run_worker, daemon=True)
    worker_thread.start()

    if not worker_ready.wait(timeout=5.0):
        print("Worker failed to connect")
        return

    print("Worker connected")

    # 3. Playwright Automation
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()

        # Register session via REST
        resp = httpx2.post(
            f"{base_url}/api/sessions",
            json={"session_id": worker_id, "display_name": "Proof", "connector_type": "websocket"},
        )
        assert resp.status_code == 200

        print(f"Navigating to {base_url}/app/operator/{worker_id}")
        page.goto(f"{base_url}/app/operator/{worker_id}")

        # Wait for terminal
        page.wait_for_selector(".xterm-helper-textarea", timeout=15000)
        print("Terminal UI ready")

        # Hijack
        print("Clicking Hijack button...")
        page.click("button:has-text('Hijack')")
        page.wait_for_selector("text=Hijacked (you)", timeout=5000)
        print("Session HIJACKED by browser")

        captured_hub._policy_gate = ProofGate()

        # 5. Type dangerous command
        print("Typing 'rm' (should trigger hold)...")
        page.click(".xterm-screen")
        page.keyboard.type("rm", delay=50)
        page.keyboard.press("Enter")

        # Wait for HOLD state
        found_pause = False
        for _ in range(50):
            if any(ws for ws in captured_hub._paused_browsers):
                found_pause = True
                break
            time.sleep(0.1)
        assert found_pause, "Browser should be paused on server"
        print("Browser PAUSED on server")

        # 6. Type buffered input
        print("Typing 'echo hello' (should be buffered)...")
        page.keyboard.type("echo hello", delay=50)
        page.keyboard.press("Enter")

        # Verify buffering
        time.sleep(1.0)
        assert any(buf for buf in captured_hub._hold_buffers.values() if "echo hello" in buf)
        print("Input BUFFERED on server")

        # 7. Resolve
        print("Resolving approval...")
        asyncio.run_coroutine_threadsafe(
            captured_hub.resolve_approval(worker_id, "proof-req", PolicyDecision(action="allow"), "rm\r"), server_loop
        ).result()

        # 8. Wait for playback on worker
        print("Waiting for playback on worker...")
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if any("echo hello" in m for m in worker_msgs):
                break
            time.sleep(0.2)

        print(f"Final worker data segments: {worker_msgs}")
        assert any("rm" in m for m in worker_msgs)
        assert any("echo hello" in m for m in worker_msgs)
        print("SUCCESS: Buffered input played back correctly.")

        browser.close()

    print("Cleaning up...")
    worker_stop.set()
    server.should_exit = True
    server_thread.join(timeout=2.0)


if __name__ == "__main__":
    run_proof()
