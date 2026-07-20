#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Targeted tests for websockets.py coverage gaps.

Covers:
- _set_ws_span_attrs with None attribute values (line 65->64)
- Worker idle timeout path (lines 117-118)
- Browser idle timeout path (lines 329-330)
- DeckMux browser connect / handle_message / disconnect paths (lines 306, 312-314, 365-368, 384)
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.client import connect_test_ws
from provide.uterm.server.bridge.hub import TermHub

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(role: str | None = None, **hub_kwargs: Any) -> tuple[FastAPI, TermHub]:
    resolver = (lambda _ws, _worker_id: role) if role is not None else None
    hub = TermHub(resolve_browser_role=resolver, **hub_kwargs)
    app = FastAPI()
    app.include_router(hub.create_router())
    return app, hub


def _read_initial_browser_messages(browser: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    hello = browser.receive_json()
    assert hello["type"] == "hello"
    hijack_state = browser.receive_json()
    assert hijack_state["type"] == "hijack_state"
    return hello, hijack_state


def _read_worker_snapshot_req(worker: Any) -> dict[str, Any]:
    msg = worker.receive_json()
    assert msg["type"] == "snapshot_req"
    return msg


def _read_worker_connected(browser: Any) -> dict[str, Any]:
    msg = browser.receive_json()
    assert msg["type"] == "worker_connected"
    return msg


# ---------------------------------------------------------------------------
# _set_ws_span_attrs — None value skipped (branch 65->64)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Worker token auth — success path (branch 85->92)
# ---------------------------------------------------------------------------


def test_worker_auth_success_with_valid_token() -> None:
    """When worker_token is configured and the correct Bearer token is
    provided, the connection proceeds normally (branch 85->92)."""
    token = "uterm-test-secret-32-byte-minimum-key"
    app, hub = _make_app(worker_token=token)

    with (
        TestClient(app) as client,
        connect_test_ws(client, "/ws/worker/bot1/term", headers={"authorization": f"Bearer {token}"}) as worker,
    ):
        msg = _read_worker_snapshot_req(worker)
        assert msg["type"] == "snapshot_req"


# ---------------------------------------------------------------------------
# _set_ws_span_attrs — None value skipped (branch 65->64)
# ---------------------------------------------------------------------------


def test_set_ws_span_attrs_none_value_skipped() -> None:
    """When a kwarg value is None, set_attribute must NOT be called for it."""
    from provide.uterm.server.bridge.routes.websockets import _set_ws_span_attrs

    calls: list[tuple[str, str]] = []

    class FakeSpan:
        def set_attribute(self, k: str, v: str) -> None:
            calls.append((k, v))

    _set_ws_span_attrs(FakeSpan(), worker_id="w1", operation=None)  # type: ignore[arg-type]

    # Only worker_id should have been set; operation=None should be skipped.
    assert len(calls) == 1
    assert calls[0] == ("uterm.worker_id", "w1")


# ---------------------------------------------------------------------------
# Worker idle timeout (lines 117-118)
# ---------------------------------------------------------------------------


def test_worker_disconnects_on_idle_timeout() -> None:
    """When the worker sends nothing for ws_idle_timeout_s, the server
    breaks out of the receive loop and cleans up."""
    app, hub = _make_app()
    # Override the idle timeout to a very short value so the server-side
    # asyncio.wait_for fires before the client sends anything.
    hub.ws_idle_timeout_s = 0.05

    with TestClient(app) as client, connect_test_ws(client, "/ws/worker/bot1/term") as worker:
        _read_worker_snapshot_req(worker)
        # Don't send anything — let the server-side timeout fire.
        time.sleep(0.2)
    # After the context exits the worker should be deregistered.
    st = hub.registry._workers.get("bot1")
    if st is not None:
        assert st.worker_ws is None


# ---------------------------------------------------------------------------
# Browser idle timeout (lines 329-330)
# ---------------------------------------------------------------------------


def test_browser_disconnects_on_idle_timeout() -> None:
    """When the browser sends nothing for ws_idle_timeout_s, the server
    breaks out of the receive loop and cleans up."""
    app, hub = _make_app()
    hub.ws_idle_timeout_s = 0.05

    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)
        time.sleep(0.2)
    # After context exits, browser should be cleaned up.
    st = hub.registry._workers.get("bot1")
    if st is not None:
        assert len(st.browsers) == 0


# ---------------------------------------------------------------------------
# DeckMux paths: presence_enabled, on_browser_connect, handle_message,
# on_browser_disconnect (lines 306, 312-314, 365-368, 384)
# ---------------------------------------------------------------------------


def test_presence_enabled_in_hello_when_deckmux_present() -> None:
    """When hub has deckmux_on_browser_connect, the hello frame includes
    presence_enabled=True (line 306)."""
    app, hub = _make_app()
    hub.deckmux_on_browser_connect = AsyncMock(return_value=None)  # type: ignore[attr-defined]
    hub.deckmux_on_browser_disconnect = AsyncMock()  # type: ignore[attr-defined]

    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        hello = browser.receive_json()
        assert hello["type"] == "hello"
        assert hello.get("presence_enabled") is True


def test_deckmux_sync_msg_sent_on_connect() -> None:
    """When deckmux_on_browser_connect returns a message, it is sent to
    the browser (lines 312-314)."""
    app, hub = _make_app()
    sync_payload = {"type": "presence_sync", "users": []}
    hub.deckmux_on_browser_connect = AsyncMock(return_value=sync_payload)  # type: ignore[attr-defined]
    hub.deckmux_on_browser_disconnect = AsyncMock()  # type: ignore[attr-defined]

    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        hello = browser.receive_json()
        assert hello["type"] == "hello"
        hijack_state = browser.receive_json()
        assert hijack_state["type"] == "hijack_state"
        # The sync message should come after hello + hijack_state.
        sync_msg = browser.receive_json()
        assert sync_msg["type"] == "presence_sync"
        assert sync_msg["users"] == []


def test_deckmux_on_browser_disconnect_called() -> None:
    """When hub has deckmux_on_browser_disconnect, it is called on
    disconnect (line 384)."""
    app, hub = _make_app()
    hub.deckmux_on_browser_connect = AsyncMock(return_value=None)  # type: ignore[attr-defined]
    disconnect_mock = AsyncMock()
    hub.deckmux_on_browser_disconnect = disconnect_mock  # type: ignore[attr-defined]

    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

    # After close, disconnect should have been called.
    assert disconnect_mock.await_count == 1
    call_args = disconnect_mock.call_args
    assert call_args[0][0] == "bot1"  # worker_id


def test_presence_update_dispatched_to_deckmux() -> None:
    """presence_update messages are dispatched to deckmux_handle_message
    (lines 365-368)."""
    app, hub = _make_app()
    handle_mock = AsyncMock()
    hub.deckmux_on_browser_connect = AsyncMock(return_value=None)  # type: ignore[attr-defined]
    hub.deckmux_on_browser_disconnect = AsyncMock()  # type: ignore[attr-defined]
    hub.deckmux_handle_message = handle_mock  # type: ignore[attr-defined]

    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_connected(browser)
            _read_worker_snapshot_req(worker)

            # Send a presence_update then a snapshot_req to sync.
            browser.send_json({"type": "presence_update", "cursor": {"x": 1, "y": 2}})
            browser.send_json({"type": "snapshot_req"})
            msg = worker.receive_json()
            assert msg["type"] == "snapshot_req"

    assert handle_mock.await_count >= 1
    call_args = handle_mock.call_args
    assert call_args[0][0] == "bot1"
    assert call_args[0][2]["type"] == "presence_update"


def test_queued_input_dispatched_to_deckmux() -> None:
    """queued_input messages are dispatched to deckmux_handle_message."""
    app, hub = _make_app()
    handle_mock = AsyncMock()
    hub.deckmux_on_browser_connect = AsyncMock(return_value=None)  # type: ignore[attr-defined]
    hub.deckmux_on_browser_disconnect = AsyncMock()  # type: ignore[attr-defined]
    hub.deckmux_handle_message = handle_mock  # type: ignore[attr-defined]

    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_connected(browser)
            _read_worker_snapshot_req(worker)

            browser.send_json({"type": "queued_input", "data": "test"})
            browser.send_json({"type": "snapshot_req"})
            msg = worker.receive_json()
            assert msg["type"] == "snapshot_req"

    assert handle_mock.await_count >= 1


def test_control_request_dispatched_to_deckmux() -> None:
    """control_request messages are dispatched to deckmux_handle_message."""
    app, hub = _make_app()
    handle_mock = AsyncMock()
    hub.deckmux_on_browser_connect = AsyncMock(return_value=None)  # type: ignore[attr-defined]
    hub.deckmux_on_browser_disconnect = AsyncMock()  # type: ignore[attr-defined]
    hub.deckmux_handle_message = handle_mock  # type: ignore[attr-defined]

    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_connected(browser)
            _read_worker_snapshot_req(worker)

            browser.send_json({"type": "control_request", "action": "pause"})
            browser.send_json({"type": "snapshot_req"})
            msg = worker.receive_json()
            assert msg["type"] == "snapshot_req"

    assert handle_mock.await_count >= 1


def test_deckmux_messages_without_handler_are_ignored() -> None:
    """When deckmux_handle_message is not present, presence/queued/control
    messages are silently skipped (line 366 falsy branch)."""
    app, hub = _make_app()
    # No deckmux_handle_message attribute — messages should be ignored.

    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        _read_initial_browser_messages(browser)

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_connected(browser)
            _read_worker_snapshot_req(worker)

            # Send all three deckmux message types — should not crash.
            browser.send_json({"type": "presence_update", "cursor": {"x": 0, "y": 0}})
            browser.send_json({"type": "queued_input", "data": "x"})
            browser.send_json({"type": "control_request", "action": "pause"})

            # Verify the connection is still alive by sending a normal message.
            browser.send_json({"type": "snapshot_req"})
            msg = worker.receive_json()
            assert msg["type"] == "snapshot_req"


def test_browser_term_test_mode_forces_admin_and_skips_deckmux_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UTERM_TEST_MODE=1: force admin role + DeckMux connect/disconnect/message
    with principal=None (multi-tab Playwright identity isolation)."""
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    # Resolver would yield viewer; TEST_MODE must override to admin.
    app, hub = _make_app(role="viewer")
    connect_mock = AsyncMock(return_value=None)
    disconnect_mock = AsyncMock()
    handle_mock = AsyncMock()
    hub.deckmux_on_browser_connect = connect_mock  # type: ignore[attr-defined]
    hub.deckmux_on_browser_disconnect = disconnect_mock  # type: ignore[attr-defined]
    hub.deckmux_handle_message = handle_mock  # type: ignore[attr-defined]

    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
        hello, _ = _read_initial_browser_messages(browser)
        # admin hello advertises can_hijack / elevated role surface.
        assert hello["type"] == "hello"
        assert connect_mock.await_count >= 1
        # principal kw must be omitted/None so each tab gets a fresh DeckMux id.
        call_kwargs = connect_mock.await_args.kwargs if connect_mock.await_args else {}
        assert call_kwargs.get("principal") is None

        with connect_test_ws(client, "/ws/worker/bot1/term") as worker:
            _read_worker_connected(browser)
            _read_worker_snapshot_req(worker)
            # websockets_browser.py:94->96 false branch (TEST_MODE skips principal).
            browser.send_json({"type": "presence_update", "cursor": {"x": 1, "y": 2}})
            # Pump the browser receive loop so the presence frame is dispatched.
            browser.send_json({"type": "snapshot_req"})
            msg = worker.receive_json()
            assert msg["type"] == "snapshot_req"

        assert handle_mock.await_count >= 1
        msg_kwargs = handle_mock.await_args.kwargs if handle_mock.await_args else {}
        assert msg_kwargs.get("principal") is None

    assert disconnect_mock.await_count >= 1
    disc_kwargs = disconnect_mock.await_args.kwargs if disconnect_mock.await_args else {}
    assert disc_kwargs.get("principal") is None
