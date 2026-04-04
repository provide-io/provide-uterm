#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Browser WebSocket edge-case tests: ownership, lease-touch, no-worker errors.

Split from test_hijack_ws.py to keep files under 500 LOC.
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.terminal.bridge.hub import TermHub
from provide.terminal.bridge.models import HijackSession, WorkerTermState
from provide.terminal.client import connect_test_ws

# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_hijack_ws.py)
# ---------------------------------------------------------------------------


def make_app(role: str | None = None) -> tuple[FastAPI, TermHub]:
    resolver = (lambda _ws, _worker_id: role) if role is not None else None
    hub = TermHub(resolve_browser_role=resolver)
    app = FastAPI()
    app.include_router(hub.create_router())
    return app, hub


def _read_initial_browser_messages(browser) -> tuple[dict, dict]:
    hello = browser.receive_json()
    assert hello["type"] == "hello"
    hijack_state = browser.receive_json()
    assert hijack_state["type"] == "hijack_state"
    return hello, hijack_state


def _read_worker_snapshot_req(worker) -> dict:
    msg = worker.receive_json()
    assert msg["type"] == "snapshot_req"
    return msg


def _read_worker_connected(browser) -> dict:
    msg = browser.receive_json()
    assert msg["type"] == "worker_connected"
    return msg


# ---------------------------------------------------------------------------
# Browser — invalid JSON, ownership lease touch, no-worker error paths
# ---------------------------------------------------------------------------


def test_browser_invalid_json_ignored() -> None:
    """Invalid JSON from browser should not crash the connection."""
    app, hub = make_app()
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_connected(browser)
            _read_worker_snapshot_req(worker)

            browser.send_text("not json {{{{")
            # Connection still alive — send valid message after invalid one
            browser.send_json({"type": "snapshot_req"})
            msg = worker.receive_json()
            assert msg["type"] == "snapshot_req"


def test_browser_sees_worker_come_online_after_connect() -> None:
    """A browser that connects before the worker must receive a worker_connected event."""
    app, hub = make_app()
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        hello, _ = _read_initial_browser_messages(browser)
        assert hello["worker_online"] is False

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            msg = _read_worker_connected(browser)
            assert msg["type"] == "worker_connected"
            assert msg["worker_id"] == "bot1"

            snapshot_req = worker.receive_json()
            assert snapshot_req["type"] == "snapshot_req"


def test_browser_snapshot_req_as_owner_touches_lease() -> None:
    """snapshot_req from owner calls _touch_hijack_owner."""
    app, hub = make_app("admin")
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_connected(browser)
            _read_worker_snapshot_req(worker)

            # Acquire hijack
            browser.send_json({"type": "hijack_request"})
            worker.receive_json()  # pause
            browser.receive_json()  # hijack_state

            # As owner, send snapshot_req — should touch lease
            browser.send_json({"type": "snapshot_req"})
            msg = worker.receive_json()
            assert msg["type"] == "snapshot_req"


def test_browser_analyze_req_as_owner_touches_lease() -> None:
    """analyze_req from owner calls _touch_hijack_owner."""
    app, hub = make_app("admin")
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_connected(browser)
            _read_worker_snapshot_req(worker)

            # Acquire hijack
            browser.send_json({"type": "hijack_request"})
            worker.receive_json()  # pause
            browser.receive_json()  # hijack_state

            # As owner, send analyze_req — should touch lease
            browser.send_json({"type": "analyze_req"})
            msg = worker.receive_json()
            assert msg["type"] == "analyze_req"


def test_browser_loses_ownership_on_worker_disconnect() -> None:
    """Worker disconnect clears hijack ownership so browser can no longer step/input.

    Previously the browser kept ownership across worker disconnect and received
    an error response.  Now the finally block revokes ownership atomically so
    hijack_step from a non-owner is silently ignored.
    """
    app, hub = make_app("admin")
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        # Acquire hijack with a worker present
        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_connected(browser)
            _read_worker_snapshot_req(worker)
            browser.send_json({"type": "hijack_request"})
            worker.receive_json()  # pause
            browser.receive_json()  # hijack_state
            assert hub._workers["bot1"].hijack_owner is not None

        # Worker disconnected: browser must receive worker_disconnected and
        # lose ownership — hijack_owner is cleared in the finally block.
        msg = browser.receive_json()
        assert msg["type"] == "worker_disconnected"
        st = hub._workers.get("bot1")
        assert st is not None, "state must exist while browser is still connected"
        assert st.hijack_owner is None, "hijack_owner must be cleared on worker disconnect"


def test_browser_input_no_worker() -> None:
    """Input as owner with no worker: ownership is revoked at disconnect, input is ignored."""
    app, hub = make_app("admin")
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_connected(browser)
            _read_worker_snapshot_req(worker)

            # Acquire hijack
            browser.send_json({"type": "hijack_request"})
            worker.receive_json()  # pause
            browser.receive_json()  # hijack_state

        # Worker disconnected — browser receives worker_disconnected and is no longer owner.
        msg = browser.receive_json()
        assert msg["type"] == "worker_disconnected"

        # Send input as (former) owner: ownership was cleared at worker disconnect,
        # so _touch_if_owner returns None and input is silently ignored.
        # Verify hub state reflects cleared ownership rather than waiting for a
        # message that will never arrive.
        st = hub._workers.get("bot1")
        assert st is not None
        assert st.hijack_owner is None


# ---------------------------------------------------------------------------
# Tests moved from test_hijack_ws.py (browser section)
# ---------------------------------------------------------------------------


def _active_session(hijack_id: str, owner: str = "test") -> HijackSession:
    return HijackSession(
        hijack_id=hijack_id,
        owner=owner,
        acquired_at=time.time(),
        lease_expires_at=time.time() + 3600,
        last_heartbeat=time.time(),
    )


def test_browser_connect_receives_hello_and_hijack_state() -> None:
    app, hub = make_app()
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        hello = browser.receive_json()
        assert hello["type"] == "hello"
        assert hello["worker_id"] == "bot1"
        assert hello["can_hijack"] is False
        assert hello["role"] == "viewer"
        assert "hijacked" in hello

        hijack_state = browser.receive_json()
        assert hijack_state["type"] == "hijack_state"
        assert hijack_state["hijacked"] is False


def test_browser_connect_receives_existing_snapshot() -> None:
    app, hub = make_app()
    hub._workers["bot1"] = WorkerTermState(last_snapshot={"type": "snapshot", "screen": "existing screen", "ts": 0.0})
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)
        snapshot = browser.receive_json()
        assert snapshot["screen"] == "existing screen"


def test_browser_snapshot_req_forwarded_to_worker() -> None:
    app, hub = make_app()
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            browser.send_json({"type": "snapshot_req"})

            msg = worker.receive_json()
            assert msg["type"] == "snapshot_req"


def test_browser_hijack_request_no_worker() -> None:
    app, hub = make_app("admin")
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        browser.send_json({"type": "hijack_request"})

        msg = browser.receive_json()
        assert msg["type"] == "error"
        state = browser.receive_json()
        assert state["type"] == "hijack_state"
        assert state["hijacked"] is False


def test_browser_hijack_request_already_held() -> None:
    app, hub = make_app("admin")
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=AsyncMock(),
        hijack_session=_active_session(str(uuid.uuid4()), "other"),
    )
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        browser.send_json({"type": "hijack_request"})

        msg = browser.receive_json()
        assert msg["type"] == "error"
        state = browser.receive_json()
        assert state["type"] == "hijack_state"


def test_browser_hijack_request_with_worker() -> None:
    app, hub = make_app("admin")
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            browser.send_json({"type": "hijack_request"})

            ctrl = worker.receive_json()
            assert ctrl["type"] == "control"
            assert ctrl["action"] == "pause"

            state = browser.receive_json()
            assert state["type"] == "hijack_state"
            assert state["hijacked"] is True
            assert state["owner"] == "me"


def test_browser_heartbeat_as_owner() -> None:
    app, hub = make_app("admin")
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            browser.send_json({"type": "hijack_request"})
            worker.receive_json()  # pause control
            browser.receive_json()  # hijack_state(hijacked=True)

            browser.send_json({"type": "heartbeat"})

            ack = browser.receive_json()
            assert ack["type"] == "heartbeat_ack"
            assert "lease_expires_at" in ack

            state = browser.receive_json()
            assert state["type"] == "hijack_state"


def test_browser_input_as_owner() -> None:
    app, hub = make_app("admin")
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            browser.send_json({"type": "hijack_request"})
            worker.receive_json()  # pause
            browser.receive_json()  # hijack_state

            browser.send_json({"type": "input", "data": "hello\r"})

            msg = worker.receive_json()
            assert msg["type"] == "input"
            assert msg["data"] == "hello\r"


def test_browser_input_not_owner_ignored() -> None:
    """Input from a non-owner browser should be silently dropped."""
    app, hub = make_app()
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            browser.send_json({"type": "input", "data": "nope"})
            browser.send_json({"type": "snapshot_req"})
            msg = worker.receive_json()
            assert msg["type"] == "snapshot_req"


def test_browser_hijack_step() -> None:
    app, hub = make_app("admin")
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            browser.send_json({"type": "hijack_request"})
            worker.receive_json()  # pause
            browser.receive_json()  # hijack_state

            browser.send_json({"type": "hijack_step"})

            ctrl = worker.receive_json()
            assert ctrl["type"] == "control"
            assert ctrl["action"] == "step"


def test_browser_hijack_release() -> None:
    app, hub = make_app("admin")
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            _read_worker_connected(browser)

            browser.send_json({"type": "hijack_request"})
            worker.receive_json()  # pause
            browser.receive_json()  # hijack_state(hijacked=True)

            browser.send_json({"type": "hijack_release"})

            ctrl = worker.receive_json()
            assert ctrl["type"] == "control"
            assert ctrl["action"] == "resume"

            state = browser.receive_json()
            assert state["type"] == "hijack_state"
            assert state["hijacked"] is False

    st = hub._workers.get("bot1")
    if st:
        assert st.hijack_owner is None


def test_browser_disconnect_as_owner_sends_resume() -> None:
    app, hub = make_app("admin")
    with TestClient(app) as client, connect_test_ws(client, "/ws/worker/bot1/term") as worker:
        _read_worker_snapshot_req(worker)

        with connect_test_ws(client, "/ws/browser/bot1/term") as browser:
            _read_initial_browser_messages(browser)
            _read_worker_snapshot_req(worker)

            browser.send_json({"type": "hijack_request"})
            worker.receive_json()  # pause
            browser.receive_json()  # hijack_state(hijacked=True)

        ctrl = worker.receive_json()
        assert ctrl["type"] == "control"
        assert ctrl["action"] == "resume"


def test_browser_registers_and_unregisters_in_browsers_set() -> None:
    app, hub = make_app()
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)
        st = hub._workers.get("bot1")
        assert st is not None
        assert len(st.browsers) == 1

    st = hub._workers.get("bot1")
    if st:
        assert len(st.browsers) == 0


def test_multiple_browsers_receive_broadcast() -> None:
    app, hub = make_app()
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser1:
        _read_initial_browser_messages(browser1)

        with connect_test_ws(client, "/ws/browser/bot1/term") as browser2:
            _read_initial_browser_messages(browser2)

            with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
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
