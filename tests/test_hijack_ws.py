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
import uuid
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.models import HijackSession, WorkerTermState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_app() -> tuple[FastAPI, TermHub]:
    hub = TermHub()
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
    with TestClient(app) as client, client.websocket_connect("/ws/worker/bot1/term") as worker:
        msg = worker.receive_json()
        assert msg["type"] == "snapshot_req"
        assert "req_id" in msg


def test_worker_registers_in_hub() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/worker/bot1/term") as worker:
        _read_worker_snapshot_req(worker)
        assert hub._workers.get("bot1") is not None
        assert hub._workers["bot1"].worker_ws is not None


def test_worker_disconnect_clears_ws() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/worker/bot1/term") as worker:
        _read_worker_snapshot_req(worker)
    # After context exits worker_ws must be cleared
    st = hub._workers.get("bot1")
    if st is not None:
        assert st.worker_ws is None


def test_worker_term_broadcast_to_browser() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            worker.send_json({"type": "term", "data": "Hello world!", "ts": 0.0})

            msg = browser.receive_json()
            assert msg["type"] == "term"
            assert msg["data"] == "Hello world!"


def test_worker_snapshot_updates_hub_state() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            worker.send_json(
                {
                    "type": "snapshot",
                    "screen": "Welcome to TW2002",
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
            assert msg["screen"] == "Welcome to TW2002"

            # Hub state updated while connections are still live.
            assert hub._workers["bot1"].last_snapshot is not None
            assert hub._workers["bot1"].last_snapshot["screen"] == "Welcome to TW2002"


def test_worker_snapshot_appends_event() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)
        with client.websocket_connect("/ws/worker/bot1/term") as worker:
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
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            worker.send_json({"type": "status", "hijacked": False, "ts": 0.0})

            msg = browser.receive_json()
            assert msg["type"] == "status"


def test_worker_analysis_broadcast() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            worker.send_json({"type": "analysis", "formatted": "Found sector 1", "raw": None, "ts": 0.0})

            msg = browser.receive_json()
            assert msg["type"] == "analysis"
            assert msg["formatted"] == "Found sector 1"


def test_worker_invalid_json_ignored() -> None:
    """Invalid JSON from the worker should not crash the connection."""
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/worker/bot1/term") as worker:
        _read_worker_snapshot_req(worker)
        worker.send_text("not json {{{{")
        # Connection still alive — valid message goes through
        worker.send_json({"type": "term", "data": "alive", "ts": 0.0})
        # No crash


# ---------------------------------------------------------------------------
# Browser WebSocket — /ws/browser/{worker_id}/term
# ---------------------------------------------------------------------------


def test_browser_connect_receives_hello_and_hijack_state() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        hello = browser.receive_json()
        assert hello["type"] == "hello"
        assert hello["worker_id"] == "bot1"
        assert hello["can_hijack"] is True
        assert "hijacked" in hello

        hijack_state = browser.receive_json()
        assert hijack_state["type"] == "hijack_state"
        assert hijack_state["hijacked"] is False


def test_browser_connect_receives_existing_snapshot() -> None:
    app, hub = make_app()
    hub._workers["bot1"] = WorkerTermState(last_snapshot={"type": "snapshot", "screen": "existing screen", "ts": 0.0})
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)
        snapshot = browser.receive_json()
        assert snapshot["screen"] == "existing screen"


def test_browser_snapshot_req_forwarded_to_worker() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            browser.send_json({"type": "snapshot_req"})

            # Worker receives a second snapshot_req from the browser's request
            msg = worker.receive_json()
            assert msg["type"] == "snapshot_req"


def test_browser_hijack_request_no_worker() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        browser.send_json({"type": "hijack_request"})

        # Error: no worker connected
        msg = browser.receive_json()
        assert msg["type"] == "error"
        # Hijack state sent after error
        state = browser.receive_json()
        assert state["type"] == "hijack_state"
        assert state["hijacked"] is False


def test_browser_hijack_request_already_held() -> None:
    app, hub = make_app()
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=AsyncMock(),
        hijack_session=_active_session(str(uuid.uuid4()), "other"),
    )
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        browser.send_json({"type": "hijack_request"})

        msg = browser.receive_json()
        assert msg["type"] == "error"
        state = browser.receive_json()
        assert state["type"] == "hijack_state"


def test_browser_hijack_request_with_worker() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            browser.send_json({"type": "hijack_request"})

            # Worker receives pause control
            ctrl = worker.receive_json()
            assert ctrl["type"] == "control"
            assert ctrl["action"] == "pause"

            # Browser receives hijack_state with owner="me"
            state = browser.receive_json()
            assert state["type"] == "hijack_state"
            assert state["hijacked"] is True
            assert state["owner"] == "me"


def test_browser_heartbeat_as_owner() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            # Acquire hijack
            browser.send_json({"type": "hijack_request"})
            worker.receive_json()  # pause control
            browser.receive_json()  # hijack_state(hijacked=True)

            # Heartbeat
            browser.send_json({"type": "heartbeat"})

            ack = browser.receive_json()
            assert ack["type"] == "heartbeat_ack"
            assert "lease_expires_at" in ack

            # Broadcast hijack_state follows heartbeat_ack
            state = browser.receive_json()
            assert state["type"] == "hijack_state"


def test_browser_input_as_owner() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            # Acquire hijack
            browser.send_json({"type": "hijack_request"})
            worker.receive_json()  # pause
            browser.receive_json()  # hijack_state

            # Send input
            browser.send_json({"type": "input", "data": "hello\r"})

            msg = worker.receive_json()
            assert msg["type"] == "input"
            assert msg["data"] == "hello\r"


def test_browser_input_not_owner_ignored() -> None:
    """Input from a non-owner browser should be silently dropped."""
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            # Don't acquire hijack — send input anyway (should be ignored)
            browser.send_json({"type": "input", "data": "nope"})
            # Follow up with something we can verify: a snapshot_req
            browser.send_json({"type": "snapshot_req"})
            msg = worker.receive_json()
            assert msg["type"] == "snapshot_req"


def test_browser_hijack_step() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            # Acquire hijack
            browser.send_json({"type": "hijack_request"})
            worker.receive_json()  # pause
            browser.receive_json()  # hijack_state

            browser.send_json({"type": "hijack_step"})

            ctrl = worker.receive_json()
            assert ctrl["type"] == "control"
            assert ctrl["action"] == "step"


def test_browser_hijack_release() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            # Acquire hijack
            browser.send_json({"type": "hijack_request"})
            worker.receive_json()  # pause
            browser.receive_json()  # hijack_state(hijacked=True)

            # Release
            browser.send_json({"type": "hijack_release"})

            ctrl = worker.receive_json()
            assert ctrl["type"] == "control"
            assert ctrl["action"] == "resume"

            state = browser.receive_json()
            assert state["type"] == "hijack_state"
            assert state["hijacked"] is False

    # Confirmed clear in hub
    st = hub._workers.get("bot1")
    if st:
        assert st.hijack_owner is None


def test_browser_disconnect_as_owner_sends_resume() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/worker/bot1/term") as worker:
        _read_worker_snapshot_req(worker)  # snapshot_req from worker connect

        with client.websocket_connect("/ws/browser/bot1/term") as browser:
            _read_initial_browser_messages(browser)
            # Browser connect triggers _request_snapshot → a second snapshot_req to worker
            _read_worker_snapshot_req(worker)

            # Acquire hijack
            browser.send_json({"type": "hijack_request"})
            worker.receive_json()  # pause
            browser.receive_json()  # hijack_state(hijacked=True)

        # Browser context exits → disconnect → resume must be sent to worker
        ctrl = worker.receive_json()
        assert ctrl["type"] == "control"
        assert ctrl["action"] == "resume"


def test_browser_registers_and_unregisters_in_browsers_set() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)
        st = hub._workers.get("bot1")
        assert st is not None
        assert len(st.browsers) == 1

    st = hub._workers.get("bot1")
    if st:
        assert len(st.browsers) == 0


def test_multiple_browsers_receive_broadcast() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser1:
        _read_initial_browser_messages(browser1)

        with client.websocket_connect("/ws/browser/bot1/term") as browser2:
            _read_initial_browser_messages(browser2)

            with client.websocket_connect("/ws/worker/bot1/term") as worker:
                _read_worker_snapshot_req(worker)
                _read_worker_connected(browser1)
                _read_worker_connected(browser2)

                worker.send_json({"type": "term", "data": "broadcast!", "ts": 0.0})

                msg1 = browser1.receive_json()
                msg2 = browser2.receive_json()
                assert msg1["type"] == "term"
                assert msg2["type"] == "term"
                assert msg1["data"] == "broadcast!"
                assert msg2["data"] == "broadcast!"
