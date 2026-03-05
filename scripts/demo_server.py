"""
Demo server for manual + Playwright testing of the provide-terminal frontend widgets.

Architecture
------------
- Real TermHub handles the browser-side WebSocket protocol.
- A simulated worker auto-connects as `/ws/worker/demo-bot/term`, responds to
  snapshot/analyze/control requests, and reconnects automatically on disconnect.
- Frontend static files served at ``/hijack/``.
- ``/hijack/hijack.html?worker=demo-bot`` (or legacy ``?bot=demo-bot``) loads
  the ProvideHijack widget connected to the real hub.

Run
---
    uv run python scripts/demo_server.py [--port PORT]
    # or via uvicorn (set DEMO_BASE_URL to match the bound address):
    DEMO_BASE_URL=http://127.0.0.1:8888 uvicorn scripts.demo_server:app --port 8888
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.resources
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from provide.terminal.hijack.hub import TermHub

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared hub (created once, reused by routes + simulated worker)
# ---------------------------------------------------------------------------

_hub = TermHub()

# ---------------------------------------------------------------------------
# Simulated demo worker
# ---------------------------------------------------------------------------

_DEMO_WORKER_ID = "demo-bot"

_DEMO_SCREEN = (
    "\x1b[1;34m[provide-terminal demo — worker: demo-bot]\x1b[0m\n"
    "─" * 60 + "\n"
    "\x1b[32mStatus:\x1b[0m  Running\n"
    "\x1b[32mSector:\x1b[0m  42\n"
    "\x1b[32mCredits:\x1b[0m 1,234,567\n"
    "\n"
    "\x1b[33mAwaiting player input...\x1b[0m\n"
    "\n"
    "Command [TL=] (?=Help)? : \x1b[7m \x1b[0m"
)

_DEMO_ANALYSIS = (
    "[demo analysis — worker: demo-bot]\n"
    "prompt_id: main_menu\n"
    "screen: 8 lines\n"
    "cursor: (30, 8)\n"
    "credits: 1,234,567"
)


def _make_snapshot(screen: str = _DEMO_SCREEN) -> dict[str, Any]:
    return {
        "type": "snapshot",
        "screen": screen,
        "cursor": {"x": 30, "y": 8},
        "cols": 80,
        "rows": 25,
        "screen_hash": "demo-hash",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": {"prompt_id": "main_menu"},
        "ts": time.time(),
    }


async def _run_demo_worker(base_url: str) -> None:
    """Continuously connect as the demo worker, auto-reconnecting on failure."""
    import websockets

    ws_url = base_url.replace("http://", "ws://") + f"/ws/worker/{_DEMO_WORKER_ID}/term"
    backoff_s = [0.5, 1.0, 2.0, 5.0, 10.0]
    attempt = 0

    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                attempt = 0
                logger.info("demo_worker_connected worker_id=%s", _DEMO_WORKER_ID)
                # Send initial snapshot immediately so browsers get live content.
                await ws.send(json.dumps(_make_snapshot()))

                paused = False
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    except asyncio.TimeoutError:
                        # Send a heartbeat snapshot to prevent idle-timeout pruning.
                        await ws.send(json.dumps(_make_snapshot()))
                        continue

                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    mtype = msg.get("type")

                    if mtype == "snapshot_req":
                        await ws.send(json.dumps(_make_snapshot()))

                    elif mtype == "analyze_req":
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "analysis",
                                    "formatted": _DEMO_ANALYSIS,
                                    "ts": time.time(),
                                }
                            )
                        )

                    elif mtype == "control":
                        action = msg.get("action")
                        if action == "pause":
                            paused = True
                            logger.debug("demo_worker_paused")
                        elif action in ("resume", "step"):
                            paused = False
                            stepped_screen = _DEMO_SCREEN + "\r\n\x1b[36m[step]\x1b[0m"
                            await ws.send(json.dumps(_make_snapshot(stepped_screen)))

                    elif mtype == "input":
                        if not paused:
                            data = msg.get("data", "")
                            # Echo input back as terminal data + refresh snapshot.
                            await ws.send(
                                json.dumps({"type": "term", "data": data, "ts": time.time()})
                            )
                            await ws.send(json.dumps(_make_snapshot()))

        except asyncio.CancelledError:
            logger.info("demo_worker_cancelled")
            return
        except Exception as exc:
            delay = backoff_s[min(attempt, len(backoff_s) - 1)]
            logger.debug("demo_worker_disconnected attempt=%d delay=%.1fs: %s", attempt, delay, exc)
            attempt += 1
            await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# FastAPI app with lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI):  # type: ignore[type-arg]
    """Start simulated demo worker after uvicorn is ready."""
    # Determine the bound port via the hub server's loop.
    # We yield first so uvicorn is fully bound before the worker connects.
    worker_task: asyncio.Task[None] | None = None
    try:
        # Small delay lets uvicorn complete its bind before the worker connects.
        async def _delayed_worker() -> None:
            await asyncio.sleep(0.3)
            await _run_demo_worker(_runtime_base_url)

        worker_task = asyncio.create_task(_delayed_worker())
        yield
    finally:
        if worker_task is not None:
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task


# The worker needs the server's bound URL.  Written by __main__ block;
# falls back to DEMO_BASE_URL env var for ``uvicorn`` invocations.
_DEFAULT_PORT = 8742
_runtime_base_url = os.environ.get("DEMO_BASE_URL", f"http://127.0.0.1:{_DEFAULT_PORT}")

app = FastAPI(lifespan=_lifespan)

# Register TermHub routes (browser WS + REST hijack).
app.include_router(_hub.create_router())

# Serve frontend static files at /hijack.
# Navigate to: /hijack/hijack.html?worker=demo-bot
frontend_path = importlib.resources.files("provide.terminal") / "frontend"
app.mount("/hijack", StaticFiles(directory=str(frontend_path), html=True), name="hijack-ui")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="provide-terminal demo server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    args = parser.parse_args()

    _runtime_base_url = f"http://{args.host}:{args.port}"
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
