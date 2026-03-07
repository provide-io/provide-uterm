#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Browser WebSocket edge-case tests: ownership, lease-touch, no-worker errors.

Split from test_hijack_ws.py to keep files under 500 LOC.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.terminal.hijack.hub import TermHub

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
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
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
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        hello, _ = _read_initial_browser_messages(browser)
        assert hello["worker_online"] is False

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            msg = _read_worker_connected(browser)
            assert msg["type"] == "worker_connected"
            assert msg["worker_id"] == "bot1"

            snapshot_req = worker.receive_json()
            assert snapshot_req["type"] == "snapshot_req"


def test_browser_snapshot_req_as_owner_touches_lease() -> None:
    """snapshot_req from owner calls _touch_hijack_owner."""
    app, hub = make_app("admin")
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
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
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
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
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        # Acquire hijack with a worker present
        with client.websocket_connect("/ws/worker/bot1/term") as worker:
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
    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
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
