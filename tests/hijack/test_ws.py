#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""WebSocket integration tests for the terminal hijack hub routes.

Uses FastAPI's synchronous TestClient with nested websocket_connect() calls so that
worker and browser connections can be open concurrently.  Each TestClient WebSocket
runs in its own thread backed by the same ASGI event-loop, so message ordering is
deterministic via internal queues.

Initial messages sent by the hub on connection
----------------------------------------------
Browser (/ws/browser/{id}/term):
  1. {"type": "hello", ...}
  2. {"type": "hijack_state", ...}
  3. last_snapshot if one exists (otherwise _request_snapshot is called, no browser msg)

Worker (/ws/worker/{id}/term):
  1. {"type": "snapshot_req", ...}   (hub requests a snapshot immediately)
"""

from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.terminal.client import connect_test_ws
from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.models import HijackSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_app(role: str | None = None) -> tuple[FastAPI, TermHub]:
    resolver = (lambda _ws, _worker_id: role) if role is not None else None
    hub = TermHub(resolve_browser_role=resolver)
    app = FastAPI()
    app.include_router(hub.create_router())
    return app, hub


def _active_session(hijack_id: str, owner: str = "test") -> HijackSession:
    return HijackSession(
        hijack_id=hijack_id,
        owner=owner,
        acquired_at=time.time(),
        lease_expires_at=time.time() + 3600,
        last_heartbeat=time.time(),
    )


def _read_initial_browser_messages(browser) -> tuple[dict, dict]:
    """Read the mandatory hello + hijack_state sent on browser connect."""
    hello = browser.receive_json()
    assert hello["type"] == "hello"
    hijack_state = browser.receive_json()
    assert hijack_state["type"] == "hijack_state"
    return hello, hijack_state


def _read_worker_snapshot_req(worker) -> dict:
    """Read the snapshot_req the hub sends immediately on worker connect."""
    msg = worker.receive_json()
    assert msg["type"] == "snapshot_req"
    return msg


def _read_worker_connected(browser) -> dict:
    """Drain the worker_connected broadcast sent to browsers on worker connect."""
    msg = browser.receive_json()
    assert msg["type"] == "worker_connected"
    return msg


# ---------------------------------------------------------------------------
# Worker WebSocket — /ws/worker/{worker_id}/term
# ---------------------------------------------------------------------------


def test_worker_connect_receives_snapshot_req() -> None:
    app, hub = make_app()
    with TestClient(app) as client, connect_test_ws(client, "/ws/worker/bot1/term") as worker:
        msg = worker.receive_json()
        assert msg["type"] == "snapshot_req"
        assert "req_id" in msg


def test_worker_registers_in_hub() -> None:
    app, hub = make_app()
    with TestClient(app) as client, connect_test_ws(client, "/ws/worker/bot1/term") as worker:
        _read_worker_snapshot_req(worker)
        assert hub._workers.get("bot1") is not None
        assert hub._workers["bot1"].worker_ws is not None


def test_worker_disconnect_clears_ws() -> None:
    app, hub = make_app()
    with TestClient(app) as client, connect_test_ws(client, "/ws/worker/bot1/term") as worker:
        _read_worker_snapshot_req(worker)
    # After context exits worker_ws must be cleared
    st = hub._workers.get("bot1")
    if st is not None:
        assert st.worker_ws is None


def test_worker_term_broadcast_to_browser() -> None:
    app, hub = make_app()
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            worker.send_json({"type": "term", "data": "Hello world!", "ts": 0.0})

            msg = browser.receive_json()
            assert msg["type"] == "term"
            assert msg["data"] == "Hello world!"


def test_worker_snapshot_updates_hub_state() -> None:
    app, hub = make_app()
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            worker.send_json(
                {
                    "type": "snapshot",
                    "screen": "Welcome to the server",
                    "cursor": {"x": 0, "y": 0},
                    "cols": 80,
                    "rows": 25,
                    "screen_hash": "abc123",
                    "cursor_at_end": True,
                    "has_trailing_space": False,
                    "prompt_detected": None,
                    "ts": time.time(),
                }
            )

            msg = browser.receive_json()
            assert msg["type"] == "snapshot"
            assert msg["screen"] == "Welcome to the server"

            # Hub state updated while connections are still live.
            assert hub._workers["bot1"].last_snapshot is not None
            assert hub._workers["bot1"].last_snapshot["screen"] == "Welcome to the server"


def test_worker_snapshot_appends_event() -> None:
    app, hub = make_app()
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)
        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)
            worker.send_json(
                {
                    "type": "snapshot",
                    "screen": "test",
                    "cursor": {"x": 0, "y": 0},
                    "cols": 80,
                    "rows": 25,
                    "screen_hash": "h1",
                    "cursor_at_end": True,
                    "has_trailing_space": False,
                    "prompt_detected": None,
                    "ts": time.time(),
                }
            )
            # Browser receiving the broadcast is the sync point that confirms
            # the server has finished processing the snapshot message.
            msg = browser.receive_json()
            assert msg["type"] == "snapshot"

            st = hub._workers.get("bot1")
            assert st is not None
            types = [e["type"] for e in st.events]
            assert "snapshot" in types


def test_worker_status_broadcast() -> None:
    app, hub = make_app()
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            worker.send_json({"type": "status", "hijacked": False, "ts": 0.0})

            msg = browser.receive_json()
            assert msg["type"] == "status"


def test_worker_analysis_broadcast() -> None:
    app, hub = make_app()
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            worker.send_json({"type": "analysis", "formatted": "Found sector 1", "raw": None, "ts": 0.0})

            msg = browser.receive_json()
            assert msg["type"] == "analysis"
            assert msg["formatted"] == "Found sector 1"


def test_worker_invalid_json_ignored() -> None:
    """Invalid JSON from the worker should not crash the connection."""
    app, hub = make_app()
    with TestClient(app) as client, connect_test_ws(client, "/ws/worker/bot1/term") as worker:
        _read_worker_snapshot_req(worker)
        worker.send_text("not json {{{{")
        # Connection still alive — valid message goes through
        worker.send_json({"type": "term", "data": "alive", "ts": 0.0})
        # No crash


# Browser WebSocket tests moved to test_hijack_ws_browser.py to keep this file under 500 LOC.
