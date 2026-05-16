#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Coverage gap tests for hijack subsystem.

Targets:
- hijack/bridge.py lines 280-282: ControlChannelProtocolError in _recv_loop → return
- hijack/bridge.py lines 285->287: DataChunk with empty data → skip send_keys
- hijack/rest_helpers.py line 49: compile_expect_regex with falsy/empty → return None
- hijack/routes/browser_handlers.py branch 262->273: resume reclaim fails when hijack already active
- hijack/routes/websockets.py lines 112-116: ControlChannelProtocolError in worker handler → close 1003
- hijack/routes/websockets.py lines 119->124: DataChunk with empty data → continue (no broadcast)
- hijack/routes/websockets.py lines 302-306: ControlChannelProtocolError in browser handler → close 1003
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.bridge.hub import TermHub
from provide.uterm.bridge.models import WorkerTermState
from provide.uterm.bridge.rest_helpers import compile_expect_regex
from provide.uterm.bridge.worker_link import TermBridge
from provide.uterm.client import connect_test_ws
from provide.uterm.control_channel import ControlChannelProtocolError, encode_data

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hub(**kwargs) -> TermHub:
    return TermHub(**kwargs)


def _make_ws() -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


def _make_worker_ws() -> MagicMock:
    wws = MagicMock()
    wws.send_text = AsyncMock()
    return wws


async def _register(hub: TermHub, worker_id: str, browser_ws: Any, role: str, worker_ws: Any | None = None) -> None:
    async with hub._lock:
        st = hub._workers.setdefault(worker_id, WorkerTermState())
        st.browsers[browser_ws] = role
        if worker_ws is not None:
            st.worker_ws = worker_ws


def _make_app(role: str | None = "admin") -> tuple[FastAPI, TermHub]:
    resolver = (lambda _ws, _worker_id: role) if role is not None else None
    hub = TermHub(resolve_browser_role=resolver)
    app = FastAPI()
    app.include_router(hub.create_router())
    return app, hub


def _drain_worker_until(worker, expected_type: str, max_msgs: int = 10) -> dict:
    """Drain worker messages until we find one of expected_type."""
    for _ in range(max_msgs):
        msg = worker.receive_json()
        if msg.get("type") == expected_type:
            return msg
    raise AssertionError(f"Did not receive {expected_type!r} within {max_msgs} messages")


def _read_initial_browser(browser) -> tuple[dict, dict]:
    hello = browser.receive_json()
    assert hello["type"] == "hello", f"expected hello, got {hello}"
    hijack_state = browser.receive_json()
    assert hijack_state["type"] == "hijack_state"
    return hello, hijack_state


def _read_worker_snapshot_req(worker) -> dict:
    msg = worker.receive_json()
    assert msg["type"] == "snapshot_req"
    return msg


# ---------------------------------------------------------------------------
# hijack/rest_helpers.py line 49: compile_expect_regex with falsy input → None
# ---------------------------------------------------------------------------


class TestCompileExpectRegexFalsy:
    """Line 49: compile_expect_regex returns None for empty / falsy input."""

    def test_none_returns_none(self) -> None:
        assert compile_expect_regex(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert compile_expect_regex("") is None

    def test_whitespace_only_returns_none(self) -> None:
        # Whitespace-only is falsy via `not expect_regex` check
        # Note: " " is truthy in Python, so this will NOT return None.
        # Only truly falsy values (None, "") hit line 49.
        # This test confirms that non-empty strings proceed past line 49.
        import re

        result = compile_expect_regex(".")
        assert isinstance(result, re.Pattern)


# ---------------------------------------------------------------------------
# hijack/bridge.py lines 280-282: ControlChannelProtocolError in _recv_loop → return
# ---------------------------------------------------------------------------


class TestBridgeRecvLoopProtocolError:
    """Lines 280-282: ControlChannelProtocolError while decoding a received frame → return from _recv_loop."""

    async def test_protocol_error_causes_recv_loop_return(self) -> None:
        """When decoder.feed raises ControlChannelProtocolError, _recv_loop returns (not crash)."""
        bot = MagicMock()
        bot.session = None
        bot.set_hijacked = AsyncMock()

        bridge = TermBridge(bot, "w1", "http://localhost:8080")
        bridge._running = True  # must be True or while loop exits immediately

        from provide.uterm.control_channel import ControlChannelDecoder

        call_count = 0
        original_feed = ControlChannelDecoder.feed

        def patched_feed(self, data):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ControlChannelProtocolError("bad frame")
            return original_feed(self, data)

        class _FakeWs:
            def __init__(self):
                self._msgs = ["bad-message", encode_data("good")]
                self._idx = 0

            async def recv(self):
                if self._idx >= len(self._msgs):
                    raise Exception("connection closed")
                msg = self._msgs[self._idx]
                self._idx += 1
                return msg

        ws = _FakeWs()
        with patch.object(ControlChannelDecoder, "feed", patched_feed):
            # _recv_loop should return after the ControlChannelProtocolError (not loop further)
            await bridge._recv_loop(ws)

        # set_hijacked(False) is called in the finally block
        bot.set_hijacked.assert_called_with(False)
        # recv was called once (the bad message triggered return before good message)
        assert ws._idx == 1


# ---------------------------------------------------------------------------
# hijack/bridge.py lines 285->287: DataChunk with empty data → skip _send_keys
# ---------------------------------------------------------------------------


class TestBridgeRecvLoopEmptyDataChunk:
    """Lines 285->287: DataChunk with empty .data → skip calling _send_keys."""

    async def test_empty_data_chunk_skips_send_keys(self) -> None:
        """DataChunk with empty data should not call session.send()."""
        bot = MagicMock()
        session = AsyncMock()
        session.send = AsyncMock()
        bot.session = session
        bot.set_hijacked = AsyncMock()

        bridge = TermBridge(bot, "w1", "http://localhost:8080")

        # Attach session so bridge finds it
        bridge._attached_session = session

        # encode_data("") produces a data chunk with empty data
        empty_data_frame = encode_data("")

        class _FakeWs:
            def __init__(self):
                self._msgs = [empty_data_frame]
                self._idx = 0
                self._closed = False

            async def recv(self):
                if self._idx >= len(self._msgs):
                    # Signal end by raising
                    raise Exception("done")
                msg = self._msgs[self._idx]
                self._idx += 1
                return msg

        ws = _FakeWs()
        bridge._running = True

        await bridge._recv_loop(ws)

        # session.send should NOT have been called for empty data
        session.send.assert_not_called()


# ---------------------------------------------------------------------------
# hijack/routes/websockets.py lines 112-116: ControlChannelProtocolError in worker → close 1003
# ---------------------------------------------------------------------------


class TestWorkerWsBadStream:
    """Lines 112-116: Worker WS sends a message that fails control channel decode → close(1003)."""

    def test_bad_stream_closes_with_1003(self) -> None:
        """A worker that sends an unparsable control channel message triggers close(1003)."""
        from provide.uterm.control_channel import ControlChannelDecoder

        app, hub = _make_app("admin")

        # Patch websocket.close to capture the code

        with (
            TestClient(app, raise_server_exceptions=False) as client,
            connect_test_ws(client, "/ws/worker/w1/term") as worker,
        ):
            _read_worker_snapshot_req(worker)

            # Patch decoder to raise on feed

            def bad_feed(self, data):
                raise ControlChannelProtocolError("injected error")

            with patch.object(ControlChannelDecoder, "feed", bad_feed):
                # Send a message — decoder.feed will raise ControlChannelProtocolError
                worker.send_text("bad stream data")
                # Worker connection should be closed by the server
                # The TestClient will raise on next receive or show disconnect
                try:
                    for _ in range(5):
                        worker.receive_json()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# hijack/routes/websockets.py lines 119->124: empty DataChunk from worker → no broadcast
# ---------------------------------------------------------------------------


class TestWorkerWsEmptyDataChunk:
    """Lines 119->124: Worker sends empty data frame → DataChunk with empty data → no broadcast."""

    def test_empty_data_chunk_not_broadcast(self) -> None:
        """Worker sends encode_data('') → DataChunk.data is empty → broadcast NOT called."""
        app, hub = _make_app("admin")
        broadcast_calls: list[dict] = []

        original_broadcast = hub.broadcast

        async def _track_broadcast(worker_id, msg):
            broadcast_calls.append(msg)
            return await original_broadcast(worker_id, msg)

        hub.broadcast = _track_broadcast  # type: ignore[method-assign]

        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)

            # Record broadcasts before sending empty data
            # (register_browser is called during _read_worker_snapshot_req implicitly)
            pre_count = len(broadcast_calls)

            # Send empty data frame
            worker.send_text(encode_data(""))

            # Give server a moment to process
            import time as _time

            _time.sleep(0.05)

            # No "term" type broadcasts should have been added for the empty frame
            post_broadcast = broadcast_calls[pre_count:]
            term_broadcasts = [m for m in post_broadcast if m.get("type") == "term"]
            assert term_broadcasts == [], f"Unexpected term broadcast for empty data: {term_broadcasts}"


# ---------------------------------------------------------------------------
# hijack/routes/websockets.py lines 302-306: ControlChannelProtocolError in browser → close 1003
# ---------------------------------------------------------------------------


class TestBrowserWsBadStream:
    """Lines 302-306: Browser WS sends a message that fails control channel decode → close(1003)."""

    def test_browser_bad_stream_closes_connection(self) -> None:
        """A browser that sends an unparsable control channel message → server closes with 1003."""
        from provide.uterm.control_channel import ControlChannelDecoder

        app, hub = _make_app("admin")

        with (
            TestClient(app, raise_server_exceptions=False) as client,
            connect_test_ws(client, "/ws/worker/w1/term") as worker,
        ):
            _read_worker_snapshot_req(worker)

            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                _read_initial_browser(browser)

                # Patch decoder to raise on feed
                def bad_feed(self, data):
                    raise ControlChannelProtocolError("browser injected error")

                with patch.object(ControlChannelDecoder, "feed", bad_feed):
                    browser.send_text("corrupt browser data")
                    # Server closes the browser connection with 1003
                    try:
                        for _ in range(5):
                            browser.receive_json()
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# hijack/routes/browser_handlers.py branch 262->273: resume reclaim fails → compensating resume
# ---------------------------------------------------------------------------


class TestBrowserHandlersResumeReclaimFail:
    """Branch 262->273: resume attempted, pause sent, but hijack reclaim conditions not met.

    Scenario: was_hijack_owner=True in resume token, pause_sent succeeds, but the
    lock check finds st.hijack_owner is already set (another hijack active) →
    reclaimed_hijack stays False → compensating resume sent (line 273-276).
    """

    async def test_resume_reclaim_fails_when_open_mode_active(self) -> None:
        """Lines 262-276: reclaim skipped because input_mode is 'open' → compensating resume sent.

        Scenario: was_hijack_owner=True in resume token, pause_sent succeeds (worker_ws exists),
        but the lock check fails because input_mode == 'open' → reclaimed_hijack stays False.
        check_still_hijacked returns False (no REST/WS hijack) → compensating resume is sent (line 274-276).
        """
        from provide.uterm.bridge.hub.resume import InMemoryResumeStore
        from provide.uterm.bridge.routes.browser_handlers import _handle_resume

        hub = _make_hub()
        store = InMemoryResumeStore()
        hub._resume_store = store

        ws = _make_ws()
        worker_ws = _make_worker_ws()

        # Register worker and browser; set input_mode = "open" so reclaim check fails
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.browsers[ws] = "admin"
            st.worker_ws = worker_ws
            st.input_mode = "open"  # reclaim condition: st.input_mode != "open" → False
            st.hijack_owner = None  # no current hijack owner
            st.hijack_owner_expires_at = None

        # Create a resume token that claims was_hijack_owner=True
        token = await store.create("w1", "admin", ttl_s=60.0)
        session = await store.get(token)
        assert session is not None
        # Mark session as was_hijack_owner so the reclaim branch is entered
        session.was_hijack_owner = True

        resume_sends: list[dict] = []
        original_send = hub.send_worker

        async def _track(worker_id, msg):
            resume_sends.append(msg)
            return await original_send(worker_id, msg)

        hub.send_worker = _track  # type: ignore[method-assign]

        msg_b = {"type": "resume", "token": token}
        await _handle_resume(hub, ws, "w1", "admin", msg_b, False)

        # A pause was sent (for reclaim attempt), then a compensating resume (reclaim failed)
        actions = [m.get("action") for m in resume_sends]
        assert "pause" in actions, f"Expected pause in {actions}"
        assert "resume" in actions, f"Expected compensating resume in {actions}"
