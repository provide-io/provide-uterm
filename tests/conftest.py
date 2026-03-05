#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Shared pytest fixtures for provide-terminal tests."""

from __future__ import annotations

import asyncio
import importlib.resources
import json
import socket
import threading
import time
from collections.abc import AsyncGenerator, Generator
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from provide.terminal.hijack.hub import TermHub

# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


@pytest.fixture()
def free_port() -> int:
    """Return a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# event_loop fixture intentionally omitted: pytest-asyncio >= 0.21 with
# asyncio_mode="auto" manages per-test loops automatically.  A custom
# event_loop fixture at function scope conflicts with the auto-mode
# machinery and can cause asyncio.Lock cross-loop corruption.


# ---------------------------------------------------------------------------
# Live TermHub — async, function-scoped (for WS integration tests)
# ---------------------------------------------------------------------------


@pytest.fixture()
async def live_hub() -> AsyncGenerator[tuple[TermHub, str], None]:
    """Async function-scoped fixture: real TermHub on a random port via uvicorn.

    Yields ``(hub, base_url)`` — e.g. ``(hub, "http://127.0.0.1:54321")``.

    The server runs as an asyncio task inside the test's event loop so that
    fixtures and tests share the same loop (important for asyncio.Lock sanity).
    """
    hub = TermHub()
    app = FastAPI()
    app.include_router(hub.create_router())

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while not server.started:
            if loop.time() > deadline:
                server.should_exit = True
                await asyncio.wait_for(task, timeout=2.0)
                raise RuntimeError("live_hub: uvicorn did not start within 5 s")
            await asyncio.sleep(0.05)

        port: int = server.servers[0].sockets[0].getsockname()[1]
        yield hub, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5.0)


# ---------------------------------------------------------------------------
# Playwright session server — sync, session-scoped
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def hijack_server() -> Generator[tuple[str, TermHub], None, None]:
    """Session-scoped sync fixture: TermHub + static UI server for Playwright tests.

    Yields ``(base_url, hub)``.

    Also exposes ``GET /test-page/{worker_id}`` — a minimal HTML page that
    mounts the ProvideHijack widget with ``heartbeatInterval: 500`` so heartbeat
    tests complete quickly.
    """
    from starlette.staticfiles import StaticFiles

    hub = TermHub()
    app = FastAPI()
    app.include_router(hub.create_router())

    frontend_path = importlib.resources.files("provide.terminal") / "frontend"
    app.mount("/ui", StaticFiles(directory=str(frontend_path), html=True), name="ui")

    @app.get("/test-page/{worker_id}", response_class=HTMLResponse)
    async def test_page(worker_id: str) -> str:
        # heartbeatInterval=500 ms so heartbeat tests don't take >5 s.
        return (
            "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
            "<style>*{margin:0;padding:0;box-sizing:border-box}"
            "html,body{width:100%;height:100dvh;background:#0b0f14}"
            "#app{width:100%;height:100%}</style></head>"
            "<body><div id='app'></div>"
            "<script src='/ui/hijack.js'></script>"
            "<script>new ProvideHijack(document.getElementById('app'),"
            f"{{workerId:{json.dumps(worker_id)},heartbeatInterval:500}});</script>"
            "</body></html>"
        )

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("hijack_server: uvicorn failed to start within 10 s")
        time.sleep(0.05)

    port: int = server.servers[0].sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    yield base_url, hub

    server.should_exit = True
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# WorkerController — background worker WS client for Playwright tests
# ---------------------------------------------------------------------------


class WorkerController:
    """Background-thread fake worker WebSocket client for Playwright tests.

    Connects to ``/ws/worker/{worker_id}/term`` on *base_url*, sends an
    initial snapshot, and collects all received messages in ``self.received``.

    Usage::

        ctrl = WorkerController(base_url, worker_id).start()
        # ... run page interactions ...
        msg = ctrl.wait_for(lambda m: m["type"] == "control", timeout=3.0)
        ctrl.stop()
    """

    def __init__(self, base_url: str, worker_id: str) -> None:
        self.received: list[dict[str, Any]] = []
        self._base_url = base_url
        self._worker_id = worker_id
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> WorkerController:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._connected.wait(timeout=5.0):
            raise RuntimeError(f"WorkerController: worker {self._worker_id!r} did not connect within 5 s")
        return self

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._connect())
        finally:
            loop.close()

    async def _connect(self) -> None:
        import websockets

        ws_url = self._base_url.replace("http://", "ws://") + f"/ws/worker/{self._worker_id}/term"
        try:
            async with websockets.connect(ws_url) as ws:
                self._connected.set()
                await ws.send(
                    json.dumps(
                        {
                            "type": "snapshot",
                            "screen": f"E2E test worker: {self._worker_id}",
                            "cursor": {"x": 0, "y": 0},
                            "cols": 80,
                            "rows": 25,
                            "screen_hash": "e2e-hash",
                            "cursor_at_end": True,
                            "has_trailing_space": False,
                            "prompt_detected": {"prompt_id": "test_prompt"},
                            "ts": time.time(),
                        }
                    )
                )
                while not self._stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.1)
                        self.received.append(json.loads(raw))
                    except TimeoutError:
                        continue
                    except Exception:
                        break
        except Exception:
            self._connected.set()  # unblock callers even on connection failure

    def wait_for(self, predicate: Any, timeout: float = 5.0) -> dict[str, Any] | None:
        """Return the first received message matching *predicate*, or None on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for msg in list(self.received):
                if predicate(msg):
                    return msg
            time.sleep(0.05)
        return None

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
