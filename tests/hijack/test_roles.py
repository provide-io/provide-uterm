#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for server-resolved browser roles (viewer / operator / admin)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from provide.terminal.client import connect_test_ws
from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.models import VALID_ROLES

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


def _make_app(
    resolver: Callable[..., str | None | Awaitable[str | None]] | None = None,
) -> tuple[FastAPI, TermHub]:
    hub = TermHub(resolve_browser_role=resolver)
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


def _drain_snapshot_req(worker) -> None:
    msg = worker.receive_json()
    assert msg["type"] == "snapshot_req"


class TestValidRoles:
    def test_valid_roles_contains_expected(self) -> None:
        assert frozenset({"viewer", "operator", "admin"}) == VALID_ROLES


class TestDefaultRole:
    def test_no_resolver_defaults_to_viewer(self) -> None:
        app, _hub = _make_app()
        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                hello, _ = _read_initial_browser(browser)
                assert hello["role"] == "viewer"
                assert hello["can_hijack"] is False

    def test_invalid_resolver_value_falls_back_to_viewer(self) -> None:
        app, _hub = _make_app(lambda _ws, _worker_id: "superuser")
        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                hello, _ = _read_initial_browser(browser)
                assert hello["role"] == "viewer"
                assert hello["can_hijack"] is False

    def test_async_resolver_supported(self) -> None:
        async def _resolve(_ws, _worker_id):
            return "operator"

        app, _hub = _make_app(_resolve)
        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                hello, _ = _read_initial_browser(browser)
                assert hello["role"] == "operator"
                assert hello["can_hijack"] is False

    def test_resolver_failure_closes_connection(self) -> None:
        def _explode(_ws, _worker_id):
            raise RuntimeError("auth backend unavailable")

        app, _hub = _make_app(_explode)
        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                try:
                    browser.receive_json()
                except WebSocketDisconnect:
                    pass
                else:
                    raise AssertionError("browser websocket should close when role resolution fails")


class TestOpenModeInput:
    def test_viewer_input_ignored_in_open_mode(self) -> None:
        app, _hub = _make_app()
        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            resp = client.post("/worker/w1/input_mode", json={"input_mode": "open"})
            assert resp.status_code == 200

            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                _read_initial_browser(browser)
                browser.send_json({"type": "input", "data": "hello"})
                browser.send_json({"type": "ping"})

    def test_operator_input_forwarded_in_open_mode(self) -> None:
        app, _hub = _make_app(lambda _ws, _worker_id: "operator")
        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            resp = client.post("/worker/w1/input_mode", json={"input_mode": "open"})
            assert resp.status_code == 200

            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                _read_initial_browser(browser)
                _drain_snapshot_req(worker)
                browser.send_json({"type": "input", "data": "hello"})
                msg = worker.receive_json()
                assert msg["type"] == "input"
                assert msg["data"] == "hello"

    def test_admin_input_forwarded_in_open_mode(self) -> None:
        app, _hub = _make_app(lambda _ws, _worker_id: "admin")
        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            resp = client.post("/worker/w1/input_mode", json={"input_mode": "open"})
            assert resp.status_code == 200

            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                _read_initial_browser(browser)
                _drain_snapshot_req(worker)
                browser.send_json({"type": "input", "data": "hello"})
                msg = worker.receive_json()
                assert msg["type"] == "input"
                assert msg["data"] == "hello"


class TestHijackPermissions:
    def test_viewer_hijack_request_rejected(self) -> None:
        app, _hub = _make_app()
        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                _read_initial_browser(browser)
                browser.send_json({"type": "hijack_request"})
                error = browser.receive_json()
                assert error["type"] == "error"
                assert "admin" in error["message"].lower()

    def test_operator_hijack_request_rejected(self) -> None:
        app, _hub = _make_app(lambda _ws, _worker_id: "operator")
        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                _read_initial_browser(browser)
                browser.send_json({"type": "hijack_request"})
                error = browser.receive_json()
                assert error["type"] == "error"
                assert "admin" in error["message"].lower()

    def test_admin_hijack_request_succeeds(self) -> None:
        app, _hub = _make_app(lambda _ws, _worker_id: "admin")
        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)
            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                hello, _ = _read_initial_browser(browser)
                assert hello["role"] == "admin"
                assert hello["can_hijack"] is True
                browser.send_json({"type": "hijack_request"})
                for _ in range(5):
                    wmsg = worker.receive_json()
                    if wmsg.get("type") == "control" and wmsg.get("action") == "pause":
                        break
                else:
                    raise AssertionError("Worker did not receive pause")
                hs = browser.receive_json()
                assert hs["type"] == "hijack_state"
                assert hs["hijacked"] is True
