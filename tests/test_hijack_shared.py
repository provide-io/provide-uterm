#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for shared-input (open) mode and input_mode switching."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.terminal.hijack.hub import TermHub

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(role: str | None = None) -> tuple[FastAPI, TermHub]:
    resolver = (lambda _ws, _worker_id: role) if role is not None else None
    hub = TermHub(resolve_browser_role=resolver)
    app = FastAPI()
    app.include_router(hub.create_router())
    return app, hub


def _read_initial_browser(browser) -> tuple[dict, dict]:
    hello = browser.receive_json()
    assert hello["type"] == "hello"
    hijack_state = browser.receive_json()
    assert hijack_state["type"] == "hijack_state"
    return hello, hijack_state


def _read_worker_snapshot_req(worker) -> dict:
    msg = worker.receive_json()
    assert msg["type"] == "snapshot_req"
    return msg


# ---------------------------------------------------------------------------
# worker_hello sets input_mode
# ---------------------------------------------------------------------------


class TestWorkerHello:
    def test_worker_hello_sets_open_mode(self) -> None:
        app, _hub = _make_app("operator")
        with TestClient(app) as client, client.websocket_connect("/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            worker.send_json({"type": "worker_hello", "input_mode": "open"})
            with client.websocket_connect("/ws/browser/w1/term") as browser:
                hello, hs = _read_initial_browser(browser)
                assert hello["input_mode"] == "open"
                assert hs["input_mode"] == "open"

    def test_worker_hello_hijack_mode(self) -> None:
        app, _hub = _make_app("admin")
        with TestClient(app) as client, client.websocket_connect("/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            worker.send_json({"type": "worker_hello", "input_mode": "hijack"})
            with client.websocket_connect("/ws/browser/w1/term") as browser:
                hello, hs = _read_initial_browser(browser)
                assert hello["input_mode"] == "hijack"
                assert hs["input_mode"] == "hijack"

    def test_no_worker_hello_defaults_hijack(self) -> None:
        app, _hub = _make_app("operator")
        with TestClient(app) as client, client.websocket_connect("/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            worker.send_json({"type": "term", "data": "hello world"})
            with client.websocket_connect("/ws/browser/w1/term") as browser:
                hello, hs = _read_initial_browser(browser)
                assert hello["input_mode"] == "hijack"
                assert hs["input_mode"] == "hijack"

    def test_worker_hello_invalid_mode_ignored(self) -> None:
        app, _hub = _make_app("admin")
        with TestClient(app) as client, client.websocket_connect("/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            worker.send_json({"type": "worker_hello", "input_mode": "bad_value"})
            with client.websocket_connect("/ws/browser/w1/term") as browser:
                hello, _hs = _read_initial_browser(browser)
                assert hello["input_mode"] == "hijack"


# ---------------------------------------------------------------------------
# Open mode: operator/admin browsers can send input
# ---------------------------------------------------------------------------


class TestOpenModeInput:
    def test_browser_sends_input_open_mode(self) -> None:
        """In open mode, an operator browser can send input without hijacking."""
        app, _hub = _make_app("operator")
        with TestClient(app) as client, client.websocket_connect("/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            # Set open mode via REST
            resp = client.post("/worker/w1/input_mode", json={"input_mode": "open"})
            assert resp.status_code == 200

            with client.websocket_connect("/ws/browser/w1/term") as b1:
                hello, _hs = _read_initial_browser(b1)
                assert hello["input_mode"] == "open"

                b1.send_json({"type": "input", "data": "from-b1"})

                # Drain messages from worker until we find the forwarded input
                for _ in range(5):
                    msg = worker.receive_json()
                    if msg["type"] == "input":
                        assert msg["data"] == "from-b1"
                        break
                else:
                    raise AssertionError("input message not received by worker")

    def test_input_ignored_in_hijack_mode_without_ownership(self) -> None:
        """In default hijack mode, a browser without ownership cannot send input."""
        app, _hub = _make_app()
        with TestClient(app) as client, client.websocket_connect("/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)

            with client.websocket_connect("/ws/browser/w1/term") as b1:
                _read_initial_browser(b1)

                # Send input without hijacking — should be silently ignored
                b1.send_json({"type": "input", "data": "should-not-arrive"})
                # Follow up with a snapshot_req we can verify goes through
                b1.send_json({"type": "snapshot_req"})
                # Drain snapshot_req(s) from worker — if input was forwarded
                # it would appear before or interleaved with snapshot_req
                found_input = False
                for _ in range(5):
                    msg = worker.receive_json()
                    if msg["type"] == "input":
                        found_input = True
                    if msg["type"] == "snapshot_req":
                        break
                assert not found_input, "input should not be forwarded without hijack ownership"


# ---------------------------------------------------------------------------
# hijack_request rejected in open mode
# ---------------------------------------------------------------------------


class TestHijackRejectedInOpenMode:
    def test_hijack_request_rejected_open_mode(self) -> None:
        app, _hub = _make_app("admin")
        with TestClient(app) as client, client.websocket_connect("/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            worker.send_json({"type": "worker_hello", "input_mode": "open"})

            with client.websocket_connect("/ws/browser/w1/term") as browser:
                _read_initial_browser(browser)

                browser.send_json({"type": "hijack_request"})
                msg = browser.receive_json()
                assert msg["type"] == "error"
                assert "open input mode" in msg["message"].lower()


# ---------------------------------------------------------------------------
# REST: input_mode endpoint
# ---------------------------------------------------------------------------


class TestRestInputMode:
    def test_set_input_mode_open(self) -> None:
        app, _hub = _make_app()
        with TestClient(app) as client, client.websocket_connect("/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            resp = client.post("/worker/w1/input_mode", json={"input_mode": "open"})
            assert resp.status_code == 200
            body = resp.json()
            assert body["ok"] is True
            assert body["input_mode"] == "open"

    def test_set_input_mode_back_to_hijack(self) -> None:
        app, _hub = _make_app()
        with TestClient(app) as client, client.websocket_connect("/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            worker.send_json({"type": "worker_hello", "input_mode": "open"})
            resp = client.post("/worker/w1/input_mode", json={"input_mode": "hijack"})
            assert resp.status_code == 200
            assert resp.json()["input_mode"] == "hijack"

    def test_set_input_mode_not_found(self) -> None:
        app, _hub = _make_app()
        with TestClient(app) as client:
            resp = client.post("/worker/noworker/input_mode", json={"input_mode": "open"})
            assert resp.status_code == 404

    def test_set_input_mode_rejected_active_hijack(self) -> None:
        """Cannot switch to open when a hijack session is active."""
        app, _hub = _make_app()
        with TestClient(app) as client, client.websocket_connect("/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            resp = client.post("/worker/w1/hijack/acquire", json={"owner": "test", "lease_s": 3600})
            assert resp.status_code == 200
            # Drain the pause sent to worker
            pause = worker.receive_json()
            assert pause["type"] == "control" and pause["action"] == "pause"

            resp = client.post("/worker/w1/input_mode", json={"input_mode": "open"})
            assert resp.status_code == 409

    def test_set_input_mode_invalid_value(self) -> None:
        app, _hub = _make_app()
        with TestClient(app) as client:
            resp = client.post("/worker/w1/input_mode", json={"input_mode": "invalid"})
            assert resp.status_code == 422

    def test_input_mode_broadcast_to_browsers(self) -> None:
        app, _hub = _make_app()
        with TestClient(app) as client, client.websocket_connect("/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)

            with client.websocket_connect("/ws/browser/w1/term") as browser:
                _read_initial_browser(browser)

                resp = client.post("/worker/w1/input_mode", json={"input_mode": "open"})
                assert resp.status_code == 200

                # Browser should receive input_mode_changed + hijack_state
                msgs = [browser.receive_json() for _ in range(2)]
                types = [m["type"] for m in msgs]
                assert "input_mode_changed" in types
                mode_msg = next(m for m in msgs if m["type"] == "input_mode_changed")
                assert mode_msg["input_mode"] == "open"


# ---------------------------------------------------------------------------
# REST: disconnect_worker endpoint
# ---------------------------------------------------------------------------


class TestRestDisconnectWorker:
    def test_disconnect_worker(self) -> None:
        app, _hub = _make_app()
        with TestClient(app) as client, client.websocket_connect("/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)

            resp = client.post("/worker/w1/disconnect_worker")
            assert resp.status_code == 200
            assert resp.json()["ok"] is True

    def test_disconnect_worker_not_found(self) -> None:
        app, _hub = _make_app()
        with TestClient(app) as client:
            resp = client.post("/worker/noworker/disconnect_worker")
            assert resp.status_code == 404
