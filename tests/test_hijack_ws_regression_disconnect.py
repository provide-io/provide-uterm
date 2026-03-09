#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Worker-disconnect critical regression tests (split from test_hijack_ws_regression.py).

Covers: browser worker_disconnected notification, stale REST/WS lease clearing on
worker disconnect, and on_hijack_changed callback correctness.
"""

from __future__ import annotations

import time
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.models import HijackSession


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
# Critical 1+2 regressions — worker disconnect: browser notification + stale
# lease clearing
# ---------------------------------------------------------------------------


def test_worker_disconnect_broadcasts_worker_disconnected_to_browsers() -> None:
    """Critical fix 1: when the worker WebSocket disconnects, all connected browsers
    must receive a worker_disconnected message so they don't hang indefinitely.

    Browser is in the outer with-block so it remains open after the worker exits.
    """
    app, hub = make_app("admin")

    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_connected(browser)
            _read_worker_snapshot_req(worker)
            # worker exits this block → disconnect

        # Worker has disconnected; browser is still open.
        msg = browser.receive_json()
        assert msg["type"] == "worker_disconnected", f"Expected worker_disconnected but got: {msg}"
        assert msg["worker_id"] == "bot1"


def test_worker_disconnect_clears_rest_hijack_session() -> None:
    """Critical fix 2: a REST hijack session must be cleared when the worker
    disconnects so a reconnecting worker is not blocked by a stale lease.
    """
    callbacks: list[tuple] = []

    def on_changed(bot_id: str, enabled: bool, owner: object) -> None:
        callbacks.append((bot_id, enabled, owner))

    hub = TermHub(on_hijack_changed=on_changed)
    fapp = FastAPI()
    fapp.include_router(hub.create_router())

    with TestClient(fapp) as client, client.websocket_connect("/ws/worker/bot1/term") as worker:
        _read_worker_snapshot_req(worker)
        session_id = str(uuid.uuid4())
        hub._workers["bot1"].hijack_session = _active_session(session_id, "rest_owner")
        assert hub._workers["bot1"].hijack_session is not None
        # worker disconnects → finally block clears hijack_session

    disabled_calls = [(b, e, o) for b, e, o in callbacks if not e]
    assert disabled_calls, "on_hijack_changed(enabled=False) must fire when worker disconnects with REST session"
    assert disabled_calls[-1][0] == "bot1"


def test_worker_disconnect_clears_ws_hijack_owner() -> None:
    """Critical fix 2: a dashboard WS hijack owner must be cleared when the worker
    disconnects so a reconnecting worker is not blocked by a stale WS lease.

    Browser is in the outer with-block so state can be inspected after worker exits.
    """
    app, hub = make_app("admin")

    with TestClient(app) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_connected(browser)
            _read_worker_snapshot_req(worker)

            browser.send_json({"type": "hijack_request"})
            msg = browser.receive_json()
            assert msg["type"] == "hijack_state", f"Expected hijack_state but got: {msg}"

            st = hub._workers.get("bot1")
            assert st is not None and st.hijack_owner is not None, "hijack_owner should be set"
            # worker exits → finally block clears hijack_owner

        msg = browser.receive_json()
        assert msg["type"] == "worker_disconnected"

        st = hub._workers.get("bot1")
        assert st is not None, "state should still exist while browser is connected"
        assert st.hijack_owner is None, "hijack_owner must be cleared on worker disconnect"
        assert st.hijack_owner_expires_at is None, "hijack_owner_expires_at must be cleared"


def test_worker_disconnect_fires_notify_when_ws_hijack_active() -> None:
    """Critical fix 2: on_hijack_changed(enabled=False) must fire when a worker
    disconnects while a WS hijack lease is active.
    """
    callbacks: list[tuple] = []

    def on_changed(bot_id: str, enabled: bool, owner: object) -> None:
        callbacks.append((bot_id, enabled, owner))

    hub = TermHub(on_hijack_changed=on_changed, resolve_browser_role=lambda _ws, _worker_id: "admin")
    fapp = FastAPI()
    fapp.include_router(hub.create_router())

    with TestClient(fapp) as client, client.websocket_connect("/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_connected(browser)
            _read_worker_snapshot_req(worker)

            browser.send_json({"type": "hijack_request"})
            msg = browser.receive_json()
            assert msg["type"] == "hijack_state"
            # worker exits with active WS hijack lease

        browser.receive_json()  # drain worker_disconnected

    disabled_calls = [(b, e, o) for b, e, o in callbacks if not e]
    assert disabled_calls, "on_hijack_changed(enabled=False) must fire when worker disconnects with WS hijack"
    assert disabled_calls[-1][0] == "bot1"


def test_worker_disconnect_no_notify_when_no_session() -> None:
    """on_hijack_changed must NOT fire when the worker disconnects with no hijack session."""
    callbacks: list[tuple] = []

    def on_changed(bot_id: str, enabled: bool, owner: object) -> None:
        callbacks.append((bot_id, enabled, owner))

    hub = TermHub(on_hijack_changed=on_changed)
    fapp = FastAPI()
    fapp.include_router(hub.create_router())

    with TestClient(fapp) as client, client.websocket_connect("/ws/worker/bot1/term") as worker:
        _read_worker_snapshot_req(worker)
        # worker disconnects with no hijack session

    disabled_calls = [(b, e, o) for b, e, o in callbacks if not e]
    assert not disabled_calls, (
        f"on_hijack_changed(enabled=False) must not fire when no session was active: {disabled_calls}"
    )
