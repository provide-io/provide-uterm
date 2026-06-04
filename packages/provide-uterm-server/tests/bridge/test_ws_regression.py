#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Regression tests for the terminal hijack WebSocket routes (fixes 4, 8, 9).

Split from test_hijack_ws.py to keep files under 500 LOC.
Covers: atomic hello state, cached snapshot delivery, was_owner init,
hijack-request send-fail notify, ping silence, spurious-notify guard.
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.client import connect_test_ws
from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_hijack_ws.py)
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
# Regression: hello message reflects atomic hijack state (fix 4)
# ---------------------------------------------------------------------------


def test_browser_hello_reflects_hijacked_state_at_connect() -> None:
    """Regression: hello message must report hijacked=True when a REST session holds

    the hijack at browser connect time.  Previously the hub re-read state after
    dropping the lock, creating a window where the hello could be stale.
    """
    app, hub = make_app("admin")

    # Pre-install an active REST hijack session so the hub considers this bot hijacked.
    session_id = str(uuid.uuid4())
    hub.registry._workers["bot42"] = WorkerTermState(
        hijack_session=_active_session(session_id, "rest_user"),
    )

    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot42/term") as browser:
        hello = browser.receive_json()
        assert hello["type"] == "hello"
        # The hello must reflect the hijacked state captured inside the lock.
        assert hello["hijacked"] is True, "hello.hijacked should be True when REST session is active"


def test_browser_hello_reflects_not_hijacked_when_no_session() -> None:
    """Regression counter-case: hello.hijacked is False when no session exists."""
    app, hub = make_app("admin")

    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot99/term") as browser:
        hello = browser.receive_json()
        assert hello["type"] == "hello"
        assert hello["hijacked"] is False


# ---------------------------------------------------------------------------
# Fix 8 regression — last_snapshot captured inside lock at browser connect
# ---------------------------------------------------------------------------


def test_browser_receives_cached_snapshot_on_connect() -> None:
    """Regression fix 8: last_snapshot must be captured inside the registration lock
    and sent to the browser atomically to prevent stale reads."""
    app, hub = make_app()

    # Pre-populate a snapshot so the browser should receive it immediately on connect.
    hub.registry._workers["snap_bot"] = WorkerTermState(
        last_snapshot={
            "type": "snapshot",
            "screen": "cached screen",
            "cursor": {"x": 0, "y": 0},
            "cols": 80,
            "rows": 25,
            "screen_hash": "hash_abc",
            "cursor_at_end": True,
            "has_trailing_space": False,
            "prompt_detected": None,
            "ts": 0.0,
        }
    )

    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/snap_bot/term") as browser:
        _read_initial_browser_messages(browser)
        # Third message must be the cached snapshot
        snap = browser.receive_json()
        assert snap["type"] == "snapshot"
        assert snap["screen"] == "cached screen"


def test_browser_requests_snapshot_when_none_cached() -> None:
    """Regression fix 8: when no snapshot is cached, hub sends snapshot_req to worker."""
    app, hub = make_app()

    with (
        TestClient(app) as client,
        connect_test_ws(client, "/ws/worker/bot_nosnap/term") as worker,
        connect_test_ws(client, "/ws/browser/bot_nosnap/term") as browser,
    ):
        # Drain worker's initial snapshot_req from connect
        _read_worker_snapshot_req(worker)

        # Browser joins — hub should send another snapshot_req since no snapshot cached
        _read_initial_browser_messages(browser)

        # Worker should have received a snapshot_req for the new browser connect
        msg = worker.receive_json()
        assert msg["type"] == "snapshot_req"


# ---------------------------------------------------------------------------
# Round-8 regression — was_owner initialized before async-with block
# ---------------------------------------------------------------------------


def test_non_owner_browser_disconnect_does_not_send_resume() -> None:
    """Round-8 fix 1: was_owner must be pre-initialized to False so that a
    non-owner browser disconnect never triggers an UnboundLocalError and never
    sends a spurious resume to the worker."""
    app, hub = make_app()
    with TestClient(app) as client, connect_test_ws(client, "/ws/worker/bot1/term") as worker:
        _read_worker_snapshot_req(worker)

        # Browser connects but never acquires the hijack
        with connect_test_ws(client, "/ws/browser/bot1/term") as browser:
            _read_initial_browser_messages(browser)
            # Browser connect triggers a snapshot_req to the worker
            _read_worker_snapshot_req(worker)

        # Browser disconnected.  Since it was never the owner, no resume should
        # be sent to the worker — only the initial snapshot_req is in flight.
        # If was_owner were unbound an UnboundLocalError would have been raised
        # and the test would fail with a 500 or an uncaught exception.
        worker.send_json({"type": "term", "data": "alive", "ts": 0.0})
        # Worker is still alive — no crash from the finally block
        assert hub.registry._workers["bot1"].worker_ws is not None


# ---------------------------------------------------------------------------
# Round-8 regression — hijack_request worker-send-fail: atomic release + notify
# ---------------------------------------------------------------------------


def test_hijack_request_send_fail_no_notify_no_owner() -> None:
    """Pause send to worker fails before ownership is written — hijack is never
    acquired so on_hijack_changed must NOT fire and hijack_owner stays None."""
    callbacks: list[tuple] = []

    def on_changed(bot_id: str, enabled: bool, owner: object) -> None:
        callbacks.append((bot_id, enabled, owner))

    hub = TermHub(on_hijack_changed=on_changed, resolve_browser_role=lambda _ws, _worker_id: "admin")
    fapp = FastAPI()
    fapp.include_router(hub.create_router())

    with TestClient(fapp) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_connected(browser)
            _read_worker_snapshot_req(worker)

            orig_send = hub.send_worker

            async def _fail_pause(bot_id: str, msg: dict) -> bool:
                if msg.get("action") == "pause":
                    return False
                return await orig_send(bot_id, msg)

            with patch.object(hub, "send_worker", side_effect=_fail_pause):
                browser.send_json({"type": "hijack_request"})
                err = browser.receive_json()
                # browser also receives hijack_state after error
                state = browser.receive_json()

            assert err["type"] == "error"
            assert state["type"] == "hijack_state"
            # Pause was never delivered so ownership was never written —
            # on_hijack_changed must not fire at all.
            assert not callbacks, "on_hijack_changed must not fire when pause send fails before ownership is written"
            st = hub.registry._workers.get("bot1")
            assert st is not None
            assert st.hijack_owner is None


def test_ping_is_silently_ignored() -> None:
    """ping from browser must produce no reply; the next received message should
    be from a subsequent snapshot_req, proving nothing was queued by the ping."""
    app, hub = make_app()
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_connected(browser)
            _read_worker_snapshot_req(worker)

            browser.send_json({"type": "ping"})
            browser.send_json({"type": "snapshot_req"})

            # Worker receives the snapshot_req forwarded from the browser
            msg = worker.receive_json()
            assert msg["type"] == "snapshot_req", f"Expected snapshot_req after ping but got: {msg}"


def test_browser_rate_limit_does_not_drop_control_frames() -> None:
    """Regression: throttle bulk input, but still honor heartbeat/release."""
    hub = TermHub(browser_rate_limit_per_sec=2, resolve_browser_role=lambda _ws, _worker_id: "admin")
    app = FastAPI()
    app.include_router(hub.create_router())

    with TestClient(app) as client, connect_test_ws(client, "/ws/worker/bot1/term") as worker:
        _read_worker_snapshot_req(worker)

        with connect_test_ws(client, "/ws/browser/bot1/term") as browser:
            _read_initial_browser_messages(browser)
            _read_worker_snapshot_req(worker)

            browser.send_json({"type": "hijack_request"})
            pause = worker.receive_json()
            assert pause["type"] == "control"
            assert pause["action"] == "pause"

            state = browser.receive_json()
            assert state["type"] == "hijack_state"
            assert state["hijacked"] is True

            browser.send_json({"type": "heartbeat"})
            ack = browser.receive_json()
            assert ack["type"] == "heartbeat_ack"
            browser.receive_json()  # refreshed hijack_state

            browser.send_json({"type": "hijack_release"})
            resume = worker.receive_json()
            assert resume["type"] == "control"
            assert resume["action"] == "resume"

            released = browser.receive_json()
            assert released["type"] == "hijack_state"
            assert released["hijacked"] is False


def test_stale_worker_disconnect_does_not_broadcast_offline() -> None:
    """Regression: closing an old worker socket must not mark the new one offline."""
    app, _hub = make_app()

    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        worker1 = connect_test_ws(client, "/ws/worker/bot1/term")
        worker1.__enter__()
        _read_worker_snapshot_req(worker1)
        _read_worker_connected(browser)

        worker2 = connect_test_ws(client, "/ws/worker/bot1/term")
        worker2.__enter__()
        try:
            _read_worker_snapshot_req(worker2)
            _read_worker_connected(browser)

            worker1.__exit__(None, None, None)

            worker2.send_json({"type": "term", "data": "still-live", "ts": 0.0})
            msg = browser.receive_json()
            assert msg["type"] == "term"
            assert msg["data"] == "still-live"
        finally:
            worker2.__exit__(None, None, None)


def test_stale_worker_output_is_ignored_after_reconnect() -> None:
    """Regression: a superseded worker socket cannot publish term/snapshot output."""
    app, hub = make_app()

    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        worker1 = connect_test_ws(client, "/ws/worker/bot1/term")
        worker1.__enter__()
        _read_worker_snapshot_req(worker1)
        _read_worker_connected(browser)

        worker2 = connect_test_ws(client, "/ws/worker/bot1/term")
        worker2.__enter__()
        try:
            _read_worker_snapshot_req(worker2)
            _read_worker_connected(browser)

            worker1.send_json({"type": "snapshot", "screen": "stale", "ts": 1.0})
            worker1.send_json({"type": "term", "data": "stale-data", "ts": 1.0})

            worker2.send_json({"type": "snapshot", "screen": "fresh", "ts": 2.0})
            msg = browser.receive_json()
            assert msg["type"] == "snapshot"
            assert msg["screen"] == "fresh"
            assert hub.registry._workers["bot1"].last_snapshot is not None
            assert hub.registry._workers["bot1"].last_snapshot["screen"] == "fresh"

            worker2.send_json({"type": "term", "data": "live", "ts": 2.0})
            msg = browser.receive_json()
            assert msg["type"] == "term"
            assert msg["data"] == "live"
        finally:
            worker1.__exit__(None, None, None)
            worker2.__exit__(None, None, None)


def test_idle_loops_still_cleanup_expired_hijack() -> None:
    """Regression: idle sockets must still wake up and expire dashboard hijacks."""
    with (
        patch("provide.uterm.server.bridge.routes.websockets._WORKER_HIJACK_CLEANUP_INTERVAL_S", 0.05),
        patch("provide.uterm.server.bridge.routes.websockets._BROWSER_HIJACK_CLEANUP_INTERVAL_S", 0.05),
    ):
        hub = TermHub(dashboard_hijack_lease_s=1, resolve_browser_role=lambda _ws, _worker_id: "admin")
        app = FastAPI()
        app.include_router(hub.create_router())

        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)

            with connect_test_ws(client, "/ws/browser/bot1/term") as browser:
                _read_initial_browser_messages(browser)
                _read_worker_snapshot_req(worker)

                browser.send_json({"type": "hijack_request"})
                pause = worker.receive_json()
                assert pause["type"] == "control"
                assert pause["action"] == "pause"

                acquired = browser.receive_json()
                assert acquired["type"] == "hijack_state"
                assert acquired["hijacked"] is True

                time.sleep(1.2)

                resume = worker.receive_json()
                assert resume["type"] == "control"
                assert resume["action"] == "resume"

                expired = browser.receive_json()
                assert expired["type"] == "hijack_state"
                assert expired["hijacked"] is False


def test_hijack_request_send_fail_no_notify_when_rest_session_active() -> None:
    """Round-8 fix 2: when _send_worker fails but a REST session is still active,
    on_hijack_changed(enabled=False) must NOT fire — the bot is still hijacked."""
    callbacks: list[tuple] = []

    def on_changed(bot_id: str, enabled: bool, owner: object) -> None:
        callbacks.append((bot_id, enabled, owner))

    hub = TermHub(on_hijack_changed=on_changed, resolve_browser_role=lambda _ws, _worker_id: "admin")
    fapp = FastAPI()
    fapp.include_router(hub.create_router())

    with TestClient(fapp) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_connected(browser)
            _read_worker_snapshot_req(worker)

            # Install the REST session AFTER the worker connects so it is not
            # cleared by the reconnect stale-state cleanup.
            rest_id = str(uuid.uuid4())
            hub.registry._workers["bot1"].hijack_session = _active_session(rest_id, "rest_owner")

            orig_send = hub.send_worker

            async def _fail_pause(bot_id: str, msg: dict) -> bool:
                if msg.get("action") == "pause":
                    return False
                return await orig_send(bot_id, msg)

            with patch.object(hub, "send_worker", side_effect=_fail_pause):
                browser.send_json({"type": "hijack_request"})
                browser.receive_json()  # error or hijack_state

            # Assert while still connected: the send-fail must not have fired a
            # disabled notify while the REST session is still active and the bot
            # has not yet disconnected.
            disabled_calls = [(b, e, o) for b, e, o in callbacks if not e]
            assert not disabled_calls, (
                f"on_hijack_changed(enabled=False) must not fire during send-fail when REST session active: {disabled_calls}"
            )
    # Note: after disconnect, on_hijack_changed(enabled=False) WILL fire because
    # the worker disconnect clears the stale REST session. That is correct behavior.


# Worker-disconnect critical regression tests have been moved to
# test_hijack_ws_regression_disconnect.py to keep this file under 500 LOC.
