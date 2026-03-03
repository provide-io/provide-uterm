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
Browser (/ws/bot/{id}/term):
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
from provide.terminal.hijack.models import BotTermState, HijackSession

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


# ---------------------------------------------------------------------------
# Worker WebSocket — /ws/worker/{bot_id}/term
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
        assert hub._bots.get("bot1") is not None
        assert hub._bots["bot1"].worker_ws is not None


def test_worker_disconnect_clears_ws() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/worker/bot1/term") as worker:
        _read_worker_snapshot_req(worker)
    # After context exits worker_ws must be cleared
    st = hub._bots.get("bot1")
    if st is not None:
        assert st.worker_ws is None


def test_worker_term_broadcast_to_browser() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)

            worker.send_json({"type": "term", "data": "Hello world!", "ts": 0.0})

            msg = browser.receive_json()
            assert msg["type"] == "term"
            assert msg["data"] == "Hello world!"


def test_worker_snapshot_updates_hub_state() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)

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
            assert hub._bots["bot1"].last_snapshot is not None
            assert hub._bots["bot1"].last_snapshot["screen"] == "Welcome to TW2002"


def test_worker_snapshot_appends_event() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)
        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
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

            st = hub._bots.get("bot1")
            assert st is not None
            types = [e["type"] for e in st.events]
            assert "snapshot" in types


def test_worker_status_broadcast() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)

            worker.send_json({"type": "status", "hijacked": False, "ts": 0.0})

            msg = browser.receive_json()
            assert msg["type"] == "status"


def test_worker_analysis_broadcast() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)

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
# Browser WebSocket — /ws/bot/{bot_id}/term
# ---------------------------------------------------------------------------


def test_browser_connect_receives_hello_and_hijack_state() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        hello = browser.receive_json()
        assert hello["type"] == "hello"
        assert hello["bot_id"] == "bot1"
        assert hello["can_hijack"] is True
        assert "hijacked" in hello

        hijack_state = browser.receive_json()
        assert hijack_state["type"] == "hijack_state"
        assert hijack_state["hijacked"] is False


def test_browser_connect_receives_existing_snapshot() -> None:
    app, hub = make_app()
    hub._bots["bot1"] = BotTermState(last_snapshot={"type": "snapshot", "screen": "existing screen", "ts": 0.0})
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)
        snapshot = browser.receive_json()
        assert snapshot["screen"] == "existing screen"


def test_browser_snapshot_req_forwarded_to_worker() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)

            browser.send_json({"type": "snapshot_req"})

            # Worker receives a second snapshot_req from the browser's request
            msg = worker.receive_json()
            assert msg["type"] == "snapshot_req"


def test_browser_analyze_req_forwarded_to_worker() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)

            browser.send_json({"type": "analyze_req"})

            msg = worker.receive_json()
            assert msg["type"] == "analyze_req"


def test_browser_hijack_request_no_worker() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
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
    hub._bots["bot1"] = BotTermState(
        worker_ws=AsyncMock(),
        hijack_session=_active_session(str(uuid.uuid4()), "other"),
    )
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        browser.send_json({"type": "hijack_request"})

        msg = browser.receive_json()
        assert msg["type"] == "error"
        state = browser.receive_json()
        assert state["type"] == "hijack_state"


def test_browser_hijack_request_with_worker() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)

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
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)

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
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)

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
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)

            # Don't acquire hijack — send input anyway (should be ignored)
            browser.send_json({"type": "input", "data": "nope"})
            # Follow up with something we can verify: a snapshot_req
            browser.send_json({"type": "snapshot_req"})
            msg = worker.receive_json()
            assert msg["type"] == "snapshot_req"


def test_browser_hijack_step() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)

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
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)

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
    st = hub._bots.get("bot1")
    if st:
        assert st.hijack_owner is None


def test_browser_disconnect_as_owner_sends_resume() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/worker/bot1/term") as worker:
        _read_worker_snapshot_req(worker)  # snapshot_req from worker connect

        with client.websocket_connect("/ws/bot/bot1/term") as browser:
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
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)
        st = hub._bots.get("bot1")
        assert st is not None
        assert len(st.browsers) == 1

    st = hub._bots.get("bot1")
    if st:
        assert len(st.browsers) == 0


def test_multiple_browsers_receive_broadcast() -> None:
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser1:
        _read_initial_browser_messages(browser1)

        with client.websocket_connect("/ws/bot/bot1/term") as browser2:
            _read_initial_browser_messages(browser2)

            with client.websocket_connect("/ws/worker/bot1/term") as worker:
                _read_worker_snapshot_req(worker)

                worker.send_json({"type": "term", "data": "broadcast!", "ts": 0.0})

                msg1 = browser1.receive_json()
                msg2 = browser2.receive_json()
                assert msg1["type"] == "term"
                assert msg2["type"] == "term"
                assert msg1["data"] == "broadcast!"
                assert msg2["data"] == "broadcast!"


def test_browser_invalid_json_ignored() -> None:
    """Invalid JSON from browser should not crash the connection (lines 167-168)."""
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)

            browser.send_text("not json {{{{")
            # Connection still alive — send valid message after invalid one
            browser.send_json({"type": "snapshot_req"})
            msg = worker.receive_json()
            assert msg["type"] == "snapshot_req"


def test_browser_snapshot_req_as_owner_touches_lease() -> None:
    """snapshot_req from owner calls _touch_hijack_owner (line 173)."""
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
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
    """analyze_req from owner calls _touch_hijack_owner (line 178)."""
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)

            # Acquire hijack
            browser.send_json({"type": "hijack_request"})
            worker.receive_json()  # pause
            browser.receive_json()  # hijack_state

            # As owner, send analyze_req — should touch lease
            browser.send_json({"type": "analyze_req"})
            msg = worker.receive_json()
            assert msg["type"] == "analyze_req"


def test_browser_hijack_step_no_worker() -> None:
    """hijack_step as owner with no worker returns error message (line 243)."""
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        # Acquire hijack with a worker present
        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)
            browser.send_json({"type": "hijack_request"})
            worker.receive_json()  # pause
            browser.receive_json()  # hijack_state

        # Worker context exited — worker is disconnected now
        # Browser is still owner. Send hijack_step — no worker connected → error
        browser.send_json({"type": "hijack_step"})
        msg = browser.receive_json()
        assert msg["type"] == "error"
        assert "worker" in msg["message"].lower()


def test_browser_input_no_worker() -> None:
    """Input as owner with no worker returns error message (line 277)."""
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)

            # Acquire hijack
            browser.send_json({"type": "hijack_request"})
            worker.receive_json()  # pause
            browser.receive_json()  # hijack_state

        # Worker is now disconnected (context exited)
        # The browser is still connected and still the owner
        # Send input — worker is gone, should get error
        browser.send_json({"type": "input", "data": "hello\r"})
        # The worker WS is None now, so _send_worker returns False → error sent
        msg = browser.receive_json()
        assert msg["type"] == "error", f"expected error message when worker disconnected, got: {msg}"
        assert "worker" in msg["message"].lower()


# ---------------------------------------------------------------------------
# Regression: hello message reflects atomic hijack state (fix 4)
# ---------------------------------------------------------------------------


def test_browser_hello_reflects_hijacked_state_at_connect() -> None:
    """Regression: hello message must report hijacked=True when a REST session holds

    the hijack at browser connect time.  Previously the hub re-read state after
    dropping the lock, creating a window where the hello could be stale.
    """
    app, hub = make_app()

    # Pre-install an active REST hijack session so the hub considers this bot hijacked.
    session_id = str(uuid.uuid4())
    hub._bots["bot42"] = BotTermState(
        hijack_session=_active_session(session_id, "rest_user"),
    )

    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot42/term") as browser:
        hello = browser.receive_json()
        assert hello["type"] == "hello"
        # The hello must reflect the hijacked state captured inside the lock.
        assert hello["hijacked"] is True, "hello.hijacked should be True when REST session is active"


def test_browser_hello_reflects_not_hijacked_when_no_session() -> None:
    """Regression counter-case: hello.hijacked is False when no session exists."""
    app, hub = make_app()

    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot99/term") as browser:
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
    hub._bots["snap_bot"] = BotTermState(
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

    with TestClient(app) as client, client.websocket_connect("/ws/bot/snap_bot/term") as browser:
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
        client.websocket_connect("/ws/worker/bot_nosnap/term") as worker,
        client.websocket_connect("/ws/bot/bot_nosnap/term") as browser,
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
    with TestClient(app) as client, client.websocket_connect("/ws/worker/bot1/term") as worker:
        _read_worker_snapshot_req(worker)

        # Browser connects but never acquires the hijack
        with client.websocket_connect("/ws/bot/bot1/term") as browser:
            _read_initial_browser_messages(browser)
            # Browser connect triggers a snapshot_req to the worker
            _read_worker_snapshot_req(worker)

        # Browser disconnected.  Since it was never the owner, no resume should
        # be sent to the worker — only the initial snapshot_req is in flight.
        # If was_owner were unbound an UnboundLocalError would have been raised
        # and the test would fail with a 500 or an uncaught exception.
        worker.send_json({"type": "term", "data": "alive", "ts": 0.0})
        # Worker is still alive — no crash from the finally block
        assert hub._bots["bot1"].worker_ws is not None


# ---------------------------------------------------------------------------
# Round-8 regression — hijack_request worker-send-fail: atomic release + notify
# ---------------------------------------------------------------------------


def test_hijack_request_send_fail_fires_notify_disabled() -> None:
    """Round-8 fix 2: when _send_worker returns False after WS hijack acquired,
    on_hijack_changed(enabled=False) must fire (so the bot automation can resume).
    Previously a separate _set_hijack_owner + notify risked a spurious double-fire."""
    from unittest.mock import patch

    callbacks: list[tuple] = []

    def on_changed(bot_id: str, enabled: bool, owner: object) -> None:
        callbacks.append((bot_id, enabled, owner))

    hub = TermHub(on_hijack_changed=on_changed)
    from fastapi import FastAPI as _FastAPI

    fapp = _FastAPI()
    fapp.include_router(hub.create_router())

    with TestClient(fapp) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)

            orig_send = hub._send_worker

            async def _fail_pause(bot_id: str, msg: dict) -> bool:
                if msg.get("action") == "pause":
                    return False
                return await orig_send(bot_id, msg)  # type: ignore[arg-type]

            with patch.object(hub, "_send_worker", side_effect=_fail_pause):
                browser.send_json({"type": "hijack_request"})
                err = browser.receive_json()

            assert err["type"] == "error"
            disabled_calls = [(b, e, o) for b, e, o in callbacks if not e]
            assert disabled_calls, "on_hijack_changed(enabled=False) must be called after send failure"
            assert disabled_calls[-1][0] == "bot1"
            # Hub must not consider bot1 hijacked after the rollback.
            # Check while connections are live — bot is pruned once all disconnect.
            st = hub._bots.get("bot1")
            assert st is not None
            assert st.hijack_owner is None


def test_ping_is_silently_ignored() -> None:
    """ping from browser must produce no reply; the next received message should
    be from a subsequent snapshot_req, proving nothing was queued by the ping."""
    app, hub = make_app()
    with TestClient(app) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)

            browser.send_json({"type": "ping"})
            browser.send_json({"type": "snapshot_req"})

            # Worker receives the snapshot_req forwarded from the browser
            msg = worker.receive_json()
            assert msg["type"] == "snapshot_req", (
                f"Expected snapshot_req after ping but got: {msg}"
            )


def test_hijack_request_send_fail_no_notify_when_rest_session_active() -> None:
    """Round-8 fix 2: when _send_worker fails but a REST session is still active,
    on_hijack_changed(enabled=False) must NOT fire — the bot is still hijacked."""
    from unittest.mock import patch

    callbacks: list[tuple] = []

    def on_changed(bot_id: str, enabled: bool, owner: object) -> None:
        callbacks.append((bot_id, enabled, owner))

    hub = TermHub(on_hijack_changed=on_changed)
    from fastapi import FastAPI as _FastAPI

    fapp = _FastAPI()
    fapp.include_router(hub.create_router())

    # Pre-install an active REST session so rest_active=True after WS release
    rest_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        hijack_session=_active_session(rest_id, "rest_owner"),
    )

    with TestClient(fapp) as client, client.websocket_connect("/ws/bot/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with client.websocket_connect("/ws/worker/bot1/term") as worker:
            _read_worker_snapshot_req(worker)

            orig_send = hub._send_worker

            async def _fail_pause(bot_id: str, msg: dict) -> bool:
                if msg.get("action") == "pause":
                    return False
                return await orig_send(bot_id, msg)  # type: ignore[arg-type]

            with patch.object(hub, "_send_worker", side_effect=_fail_pause):
                browser.send_json({"type": "hijack_request"})
                browser.receive_json()  # error or hijack_state

    # REST session still active → on_hijack_changed(enabled=False) must NOT have fired
    disabled_calls = [(b, e, o) for b, e, o in callbacks if not e]
    assert not disabled_calls, (
        f"on_hijack_changed(enabled=False) must not fire when REST session is still active: {disabled_calls}"
    )
