"""
Interactive example server for manual and Playwright testing of the hijack UI.

Architecture
------------
- Real TermHub handles the browser-side WebSocket protocol.
- An in-memory session worker auto-connects as ``/ws/worker/provide-shell/term``.
- The worker renders a deterministic interactive transcript, responds to
  snapshot/analyze/control requests, and reconnects automatically on disconnect.
- Frontend static files are served at ``/hijack/``.
- ``/hijack/hijack.html?worker=provide-shell`` loads the interactive example page.

Run
---
    uv run python scripts/example_server.py [--port PORT]
    # or via uvicorn (set EXAMPLE_BASE_URL to match the bound address):
    EXAMPLE_BASE_URL=http://127.0.0.1:8888 uvicorn scripts.example_server:app --port 8888
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.resources
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Body, FastAPI, HTTPException
from starlette.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _example_session import (
    _DEFAULT_PORT,
    DemoSessionState,
    _append_entry,
    _enqueue_worker_messages,
    _force_release_hijack_for_shared_mode,
    _get_or_create_session,
    _hub,
    _reset_session_state,
    _session_payload,
    _set_input_mode,
    _start_default_session_workers,
    _state_update_messages,
    _sync_hub_input_mode,
)
from _example_session import (
    _DEFAULT_WORKER_ID as _DEFAULT_WORKER_ID,  # re-export for test access
)
from _example_session import (
    _apply_control as _apply_control,  # re-export for test access
)
from _example_session import (
    _apply_input as _apply_input,  # re-export for test access
)
from _example_session import (
    _make_analysis as _make_analysis,  # re-export for test access
)
from _example_session import (
    _make_snapshot as _make_snapshot,  # re-export for test access
)
from _example_session import (
    _reset_all_sessions as _reset_all_sessions,  # re-export for test access
)

logger = logging.getLogger(__name__)

_runtime_base_url = os.environ.get("EXAMPLE_BASE_URL", f"http://127.0.0.1:{_DEFAULT_PORT}")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Start the built-in demo session worker after uvicorn is ready."""
    worker_tasks: list[asyncio.Task[None]] = []
    try:

        async def _delayed_start() -> None:
            await asyncio.sleep(0.3)
            workers = _start_default_session_workers(_runtime_base_url)
            worker_tasks.extend(workers)
            await asyncio.gather(*workers)

        root_task = asyncio.create_task(_delayed_start())
        worker_tasks.append(root_task)
        yield
    finally:
        for task in worker_tasks:
            task.cancel()
        for task in worker_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(lifespan=_lifespan)
app.include_router(_hub.create_router())


@app.get("/demo/session/{worker_id}")
async def get_demo_session(worker_id: str) -> dict[str, Any]:
    """Example-only debug endpoint for inspecting demo session state."""
    return _session_payload(_get_or_create_session(worker_id))


@app.post("/demo/session/{worker_id}/mode")
async def set_demo_session_mode(worker_id: str, payload: Annotated[dict[str, str], Body(...)]) -> dict[str, Any]:
    """Example-only endpoint for switching the demo session input mode."""
    mode = payload.get("input_mode", "").strip().lower()
    if mode not in {"hijack", "open"}:
        raise HTTPException(status_code=400, detail="input_mode must be 'hijack' or 'open'")
    session = _get_or_create_session(worker_id)
    if mode == "open":
        released = await _force_release_hijack_for_shared_mode(worker_id)
        if released:
            session.paused = False
            session.status_line = "Live"
            session.pending_banner = "Switched to shared input. Active hijack released."
            _append_entry(session, "system", "control: released for shared input")
    messages = _set_input_mode(session, mode, source="http")
    await _sync_hub_input_mode(worker_id, mode)
    _enqueue_worker_messages(session, messages)
    return _session_payload(session)


@app.post("/demo/session/{worker_id}/reset")
async def reset_demo_session(worker_id: str) -> dict[str, Any]:
    """Example-only endpoint for clearing the demo session transcript."""
    session = _reset_session_state(worker_id)
    session.analysis_note = "reset from http"
    session.pending_banner = "Session reset."
    messages = _state_update_messages(session)
    _enqueue_worker_messages(session, messages)
    return _session_payload(session)


def _session_status_payload(session: DemoSessionState) -> dict[str, Any]:
    """Return a SessionStatus-compatible payload for the standard /api/sessions/ routes."""
    return {
        "session_id": session.worker_id,
        "display_name": session.title,
        "connector_type": "shell",
        "lifecycle_state": "paused" if session.paused else "running",
        "input_mode": session.input_mode,
        "connected": session.connected,
        "auto_start": True,
        "tags": [],
        "recording_enabled": False,
        "recording_available": False,
        "owner": None,
        "visibility": "public",
        "last_error": None,
    }


@app.get("/api/sessions/{worker_id}")
async def get_session_status(worker_id: str) -> dict[str, Any]:
    return _session_status_payload(_get_or_create_session(worker_id))


@app.post("/api/sessions/{worker_id}/mode")
async def set_session_mode(worker_id: str, payload: Annotated[dict[str, str], Body(...)]) -> dict[str, Any]:
    mode = payload.get("input_mode", "").strip().lower()
    if mode not in {"hijack", "open"}:
        raise HTTPException(status_code=400, detail="input_mode must be 'hijack' or 'open'")
    session = _get_or_create_session(worker_id)
    if mode == "open":
        released = await _force_release_hijack_for_shared_mode(worker_id)
        if released:
            session.paused = False
            session.status_line = "Live"
            session.pending_banner = "Switched to shared input. Active hijack released."
            _append_entry(session, "system", "control: released for shared input")
    messages = _set_input_mode(session, mode, source="http")
    await _sync_hub_input_mode(worker_id, mode)
    _enqueue_worker_messages(session, messages)
    return _session_status_payload(session)


@app.post("/api/sessions/{worker_id}/restart")
async def restart_session(worker_id: str) -> dict[str, Any]:
    session = _reset_session_state(worker_id)
    session.analysis_note = "reset from http"
    session.pending_banner = "Session reset."
    messages = _state_update_messages(session)
    _enqueue_worker_messages(session, messages)
    return _session_status_payload(session)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> dict[str, bool]:
    return {"ok": True}


frontend_path = importlib.resources.files("provide.terminal.server") / "frontend"
app.mount("/hijack", StaticFiles(directory=str(frontend_path), html=True), name="hijack-ui")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="provide-uterm interactive example server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    args = parser.parse_args()

    _runtime_base_url = f"http://{args.host}:{args.port}"
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
