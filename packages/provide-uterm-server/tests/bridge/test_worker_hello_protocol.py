#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""WS handler-level tests for worker_hello range negotiation.

The handler must:
- accept legacy ``protocol_version: int`` (treated as ``min=max=v``)
- accept the new ``protocol: {min, max, preferred}`` block
- default missing fields to ``{min=1, max=1}``
- store the negotiated version on WorkerTermState
- close with code 1002 + emit ``reason=protocol_mismatch`` when ranges don't overlap
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.bridge.contracts import MAX_PROTOCOL_VERSION
from provide.uterm.client import connect_test_ws
from provide.uterm.server.bridge.hub import TermHub


def _make_app() -> tuple[FastAPI, TermHub]:
    hub = TermHub(resolve_browser_role=lambda _ws, _worker_id: "operator")
    app = FastAPI()
    app.include_router(hub.create_router())
    return app, hub


def _read_first_browser_frame(ws: Any) -> dict[str, Any]:
    """Drain initial browser frames until the first non-empty frame arrives."""
    for _ in range(5):
        msg = ws.receive_json()
        if isinstance(msg, dict) and msg.get("type"):
            return msg
    raise AssertionError("no browser frame within 5 messages")


class TestWorkerHelloNegotiation:
    def _capture_state_after_hello(self, hub: TermHub, worker_id: str) -> Any:
        """Snapshot the WorkerTermState while the WS is still registered.

        The hub deregisters the worker on disconnect, so any assertion that
        runs after the WS context manager exits will see ``None``. Polling
        from inside the context, returning a snapshot, lets the test
        assert on the negotiated version after teardown.
        """
        import time as _time

        # Give the server's worker_hello handler time to commit.
        for _ in range(20):
            st = hub._workers.get(worker_id)  # type: ignore[attr-defined]
            if st is not None and st.protocol_version is not None:
                return st
            _time.sleep(0.02)
        return hub._workers.get(worker_id)  # type: ignore[attr-defined]

    def test_legacy_protocol_version_int_is_accepted(self) -> None:
        """Old workers sent ``protocol_version: 1``; that must still work."""
        app, hub = _make_app()
        captured: list[Any] = []
        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/legacy/term") as worker:
            worker.receive_json()  # snapshot_req
            worker.send_json({"type": "worker_hello", "input_mode": "open", "protocol_version": 1})
            captured.append(self._capture_state_after_hello(hub, "legacy"))
        st = captured[0]
        assert st is not None
        assert st.protocol_version == 1

    def test_protocol_block_is_accepted(self) -> None:
        app, hub = _make_app()
        captured: list[Any] = []
        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/proto/term") as worker:
            worker.receive_json()
            worker.send_json(
                {
                    "type": "worker_hello",
                    "input_mode": "hijack",
                    "protocol": {"min": 1, "max": 1, "preferred": 1},
                }
            )
            captured.append(self._capture_state_after_hello(hub, "proto"))
        st = captured[0]
        assert st is not None
        assert st.protocol_version == 1
        assert st.input_mode == "hijack"

    def test_missing_protocol_defaults_to_v1(self) -> None:
        """Pre-negotiation workers omit any protocol field; default to {1,1}."""
        app, hub = _make_app()
        captured: list[Any] = []
        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/silent/term") as worker:
            worker.receive_json()
            worker.send_json({"type": "worker_hello", "input_mode": "open"})
            captured.append(self._capture_state_after_hello(hub, "silent"))
        st = captured[0]
        assert st is not None
        # Default negotiated version is the server max under the {1,1} client range.
        assert st.protocol_version == MAX_PROTOCOL_VERSION

    def test_no_overlap_closes_with_error_frame(self) -> None:
        """Worker claims it can only speak v99-100; server is v1 → close 1002."""
        import pytest
        from starlette.websockets import WebSocketDisconnect

        app, _hub = _make_app()
        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/mismatch/term") as worker:
            worker.receive_json()
            worker.send_json(
                {
                    "type": "worker_hello",
                    "input_mode": "open",
                    "protocol": {"min": 99, "max": 100, "preferred": 99},
                }
            )
            # Server should send an error frame then close with 1002.
            err = worker.receive_json()
            assert err.get("type") == "error"
            assert err.get("reason") == "protocol_mismatch"
            assert err.get("client_min") == 99
            assert err.get("client_max") == 100
            assert err.get("server_min") == 1
            assert err.get("server_max") == MAX_PROTOCOL_VERSION
            # Next receive should disconnect.
            with pytest.raises(WebSocketDisconnect) as excinfo:
                worker.receive_json()
        assert excinfo.value.code == 1002


class TestBrowserHelloAdvertisesRange:
    def test_browser_hello_includes_protocol_block(self) -> None:
        app, hub = _make_app()
        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/w1/term") as worker:
            worker.receive_json()  # snapshot_req
            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                hello = _read_first_browser_frame(browser)
                # First non-empty frame should be the hello with the new protocol block.
                # If the first frame is hijack_state, drain another.
                if hello.get("type") != "hello":
                    hello = _read_first_browser_frame(browser)
                assert hello["type"] == "hello"
                assert "protocol" in hello
                proto = hello["protocol"]
                assert proto["selected"] == 1
                assert proto["server_min"] == 1
                assert proto["server_max"] == MAX_PROTOCOL_VERSION
