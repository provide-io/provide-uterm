#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for WebSocket message size limits and rate limiting."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.bridge.hub import TermHub
from provide.uterm.bridge.ratelimit import TokenBucket
from provide.uterm.client import connect_test_ws

# ---------------------------------------------------------------------------
# TokenBucket unit tests
# ---------------------------------------------------------------------------


class TestTokenBucket:
    def test_allows_burst(self) -> None:
        bucket = TokenBucket(10, burst=3)
        assert bucket.allow()
        assert bucket.allow()
        assert bucket.allow()
        # Burst exhausted
        assert not bucket.allow()

    def test_defaults_burst_to_rate(self) -> None:
        bucket = TokenBucket(5)
        for _ in range(5):
            assert bucket.allow()
        assert not bucket.allow()

    def test_refills_over_time(self) -> None:
        import time

        bucket = TokenBucket(100, burst=1)
        assert bucket.allow()
        assert not bucket.allow()
        time.sleep(0.02)
        assert bucket.allow()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(**hub_kwargs) -> tuple[FastAPI, TermHub]:
    if "resolve_browser_role" not in hub_kwargs:
        hub_kwargs["resolve_browser_role"] = lambda _ws, _worker_id: "operator"
    hub = TermHub(**hub_kwargs)
    app = FastAPI()
    app.include_router(hub.create_router())
    return app, hub


def _read_worker_snapshot_req(worker) -> dict:
    msg = worker.receive_json()
    assert msg["type"] == "snapshot_req"
    return msg


def _read_initial_browser(browser) -> tuple[dict, dict]:
    hello = browser.receive_json()
    assert hello["type"] == "hello"
    hijack_state = browser.receive_json()
    assert hijack_state["type"] == "hijack_state"
    return hello, hijack_state


# ---------------------------------------------------------------------------
# Browser: oversized message → silently dropped
# ---------------------------------------------------------------------------


class TestBrowserOversizedMessage:
    def test_oversized_browser_message_dropped(self) -> None:
        import json as _json

        # min clamp is 1024, so use 1024 and send > 1024 bytes
        app, hub = _make_app(max_ws_message_bytes=1024)

        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            # Set open mode so browser can send input
            resp = client.post("/worker/w1/input_mode", json={"input_mode": "open"})
            assert resp.status_code == 200

            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                _read_initial_browser(browser)

                # Send oversized message (> 1024 bytes, dropped by handler)
                big = _json.dumps({"type": "input", "data": "x" * 2000})
                browser.send_text(big)

                # Follow up with a normal-sized input that goes through
                browser.send_json({"type": "input", "data": "ok"})

                # Worker should only see the "ok" input, not the oversized one
                for _ in range(10):
                    msg = worker.receive_json()
                    if msg.get("type") == "input":
                        assert msg["data"] == "ok"
                        break
                else:
                    raise AssertionError("input message not received by worker")


# ---------------------------------------------------------------------------
# Browser: rate limit exceeded → dropped
# ---------------------------------------------------------------------------


class TestBrowserRateLimit:
    def test_rate_limited_browser_messages_dropped(self) -> None:
        app, hub = _make_app(browser_rate_limit_per_sec=2)

        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            # Set open mode
            resp = client.post("/worker/w1/input_mode", json={"input_mode": "open"})
            assert resp.status_code == 200

            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                _read_initial_browser(browser)

                # Exhaust the bucket (burst=2) with real input
                browser.send_json({"type": "input", "data": "1"})
                browser.send_json({"type": "input", "data": "2"})
                # Third should be rate-limited (silently dropped)
                browser.send_json({"type": "input", "data": "dropped"})

                import time

                time.sleep(0.6)  # let bucket refill
                browser.send_json({"type": "input", "data": "after"})

                # Collect inputs received by worker
                inputs = []
                for _ in range(15):
                    msg = worker.receive_json()
                    if msg.get("type") == "input":
                        inputs.append(msg["data"])
                        if msg["data"] == "after":
                            break
                # "1" and "2" should arrive, "dropped" should not
                assert "1" in inputs
                assert "2" in inputs
                assert "dropped" not in inputs
                assert "after" in inputs


# ---------------------------------------------------------------------------
# Browser: input too long → error
# ---------------------------------------------------------------------------


class TestBrowserInputTooLong:
    def test_input_too_long_returns_error(self) -> None:
        app, hub = _make_app(max_input_chars=200)
        assert hub.max_input_chars == 200

        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            # Set open mode so browser can send input
            resp = client.post("/worker/w1/input_mode", json={"input_mode": "open"})
            assert resp.status_code == 200

            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                _read_initial_browser(browser)

                browser.send_json({"type": "input", "data": "a" * 201})
                error = browser.receive_json()
                assert error["type"] == "error"
                assert "too long" in error["message"].lower()


# ---------------------------------------------------------------------------
# Worker: oversized message → silently dropped
# ---------------------------------------------------------------------------


class TestWorkerOversizedMessage:
    def test_oversized_worker_message_dropped(self) -> None:
        # min clamp is 1024
        app, hub = _make_app(max_ws_message_bytes=1024)

        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)

            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                _read_initial_browser(browser)

                # Send normal-sized term data (should go through)
                worker.send_json({"type": "term", "data": "hi"})
                msg = browser.receive_json()
                assert msg["type"] == "term"
                assert msg["data"] == "hi"

                # Send oversized data > 1024 bytes (should be dropped)
                worker.send_json({"type": "term", "data": "x" * 2000})
                # Follow up with a small message to verify dropped
                worker.send_json({"type": "term", "data": "ok"})
                msg2 = browser.receive_json()
                assert msg2["type"] == "term"
                assert msg2["data"] == "ok"


# ---------------------------------------------------------------------------
# Worker: bursts continue to flow (no worker rate limit)
# ---------------------------------------------------------------------------


class TestWorkerBursts:
    def test_worker_term_burst_not_dropped(self) -> None:
        app, hub = _make_app()

        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)

            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                _read_initial_browser(browser)

                worker.send_json({"type": "term", "data": "1"})
                worker.send_json({"type": "term", "data": "2"})
                worker.send_json({"type": "term", "data": "3"})

                msgs = [browser.receive_json(), browser.receive_json(), browser.receive_json()]
                assert [msg["data"] for msg in msgs] == ["1", "2", "3"]


# ---------------------------------------------------------------------------
# Custom limits via constructor
# ---------------------------------------------------------------------------


class TestCustomLimits:
    def test_custom_limits_applied(self) -> None:
        hub = TermHub(
            max_ws_message_bytes=2048,
            max_input_chars=500,
            browser_rate_limit_per_sec=10,
            resolve_browser_role=lambda _ws, _worker_id: "operator",
        )
        assert hub.max_ws_message_bytes == 2048
        assert hub.max_input_chars == 500
        assert hub.browser_rate_limit_per_sec == 10.0

    def test_min_ws_message_bytes_clamped(self) -> None:
        hub = TermHub(max_ws_message_bytes=10)
        assert hub.max_ws_message_bytes == 1024  # minimum is 1024

    def test_min_input_chars_clamped(self) -> None:
        hub = TermHub(max_input_chars=10)
        assert hub.max_input_chars == 100  # minimum is 100
