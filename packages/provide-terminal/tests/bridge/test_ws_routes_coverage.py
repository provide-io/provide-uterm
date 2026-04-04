#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Targeted tests for websockets.py edge paths (ws:58-61, 65-66, 286, 323, 386)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.terminal.bridge.hub import TermHub
from provide.terminal.bridge.models import HijackSession, WorkerTermState
from provide.terminal.client import connect_test_ws


def _make_app(role: str | None = None) -> tuple[FastAPI, TermHub]:
    resolver = (lambda _ws, _worker_id: role) if role is not None else None
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


# ---------------------------------------------------------------------------
# ws:58-61, 65-66 — Stale hijack cleared on worker reconnect
# ---------------------------------------------------------------------------


class TestStaleHijackOnWorkerReconnect:
    """When a worker reconnects and the old connection had an active hijack,
    the hub clears the stale hijack state (lines 58-61) and fires
    _notify_hijack_changed + _broadcast_hijack_state (lines 65-66)."""

    def test_worker_reconnect_clears_stale_rest_hijack(self) -> None:
        app, hub = _make_app("admin")
        hijack_calls: list[dict] = []
        hub._on_hijack_changed = lambda wid, enabled, owner: hijack_calls.append({"worker_id": wid, "enabled": enabled})

        with TestClient(app) as client:
            # Manually set up stale hijack state (simulates a crashed worker
            # that left hijack state behind)
            import asyncio

            now = time.time()

            async def _setup_stale():
                async with hub._lock:
                    st = hub._workers.setdefault("w1", WorkerTermState())
                    st.hijack_session = HijackSession(
                        hijack_id="stale-hid",
                        owner="stale",
                        acquired_at=now,
                        lease_expires_at=now + 300,
                        last_heartbeat=now,
                    )
                    st.hijack_owner = AsyncMock()
                    st.hijack_owner_expires_at = now + 300

            asyncio.run(_setup_stale())

            # Connect a new worker — should clear stale hijack (lines 58-61, 65-66)
            with connect_test_ws(client, "/ws/worker/w1/term") as worker:
                _read_worker_snapshot_req(worker)
                # The stale hijack should have been cleared
                assert len(hijack_calls) == 1
                assert hijack_calls[0]["enabled"] is False


# ---------------------------------------------------------------------------
# ws:286 — Compensating resume on no_worker
# ---------------------------------------------------------------------------


class TestCompensatingResumeNoWorker:
    """ws:286 — When _try_acquire_ws_hijack returns (False, 'no_worker'),
    a compensating resume is sent and the browser gets an error."""

    def test_hijack_request_no_worker_sends_resume(self) -> None:
        app, hub = _make_app("admin")
        resume_sent = []

        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)

            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                _read_initial_browser(browser)

                # Patch _try_acquire_ws_hijack to return no_worker
                # and track _send_worker calls for the resume
                _orig_send = hub.send_worker

                async def _track_send(wid, msg):
                    if msg.get("action") == "resume":
                        resume_sent.append(msg)
                    return await _orig_send(wid, msg)

                with (
                    patch.object(
                        hub,
                        "try_acquire_ws_hijack",
                        return_value=(False, "no_worker"),
                    ),
                    patch.object(hub, "send_worker", side_effect=_track_send),
                ):
                    browser.send_json({"type": "hijack_request"})
                    # Browser gets error
                    error = browser.receive_json()
                    assert error["type"] == "error"
                    assert "no worker" in error["message"].lower()

                # Compensating resume was called (line 286)
                assert len(resume_sent) == 1
                assert resume_sent[0]["action"] == "resume"


# ---------------------------------------------------------------------------
# ws:323 — hijack_step send failure
# ---------------------------------------------------------------------------


class TestHijackStepSendFailure:
    """ws:323 — hijack_step fails to send to worker → error message."""

    def test_step_send_failure_returns_error(self) -> None:
        app, hub = _make_app("admin")

        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)

            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                _read_initial_browser(browser)

                # Hijack first
                browser.send_json({"type": "hijack_request"})
                # Drain messages from worker until we find pause
                for _ in range(5):
                    wmsg = worker.receive_json()
                    if wmsg.get("type") == "control" and wmsg.get("action") == "pause":
                        break
                else:
                    raise AssertionError("pause not received by worker")
                # Browser gets hijack_state
                hs = browser.receive_json()
                assert hs["type"] == "hijack_state"

                # Patch _send_worker to fail for step
                async def _fail_step(wid, msg):
                    return msg.get("action") != "step"

                with patch.object(hub, "send_worker", side_effect=_fail_step):
                    browser.send_json({"type": "hijack_step"})
                    error = browser.receive_json()
                    assert error["type"] == "error"
                    assert "no worker" in error["message"].lower()


# ---------------------------------------------------------------------------
# ws:386 — Input send failure in open mode
# ---------------------------------------------------------------------------


class TestInputSendFailureOpenMode:
    """ws:386 — input send fails in open mode → error message."""

    def test_input_send_failure_returns_error(self) -> None:
        app, hub = _make_app("operator")

        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/w1/term") as worker:
            _read_worker_snapshot_req(worker)

            # Set open mode
            resp = client.post("/worker/w1/input_mode", json={"input_mode": "open"})
            assert resp.status_code == 200

            with connect_test_ws(client, "/ws/browser/w1/term") as browser:
                _read_initial_browser(browser)

                # Patch _send_worker to fail for input
                async def _fail_input(wid, msg):
                    return msg.get("type") != "input"

                with patch.object(hub, "send_worker", side_effect=_fail_input):
                    browser.send_json({"type": "input", "data": "test-data"})
                    error = browser.receive_json()
                    assert error["type"] == "error"
                    assert "worker" in error["message"].lower()


# ---------------------------------------------------------------------------
# Snapshot field safety: ts=None / negative cols / negative rows
# ---------------------------------------------------------------------------


class TestSnapshotFieldSafety:
    """Worker snapshot messages with invalid ts / cols / rows are stored safely."""

    def _make_app_and_register(self) -> tuple[TestClient, TermHub, object]:
        app, hub = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        worker = connect_test_ws(client, "/ws/worker/w1/term?token=x")
        worker.__enter__()
        _read_worker_snapshot_req(worker)
        return client, hub, worker

    def test_ts_none_stored_as_float(self) -> None:
        """ts=None from worker is coerced to a float; wait_for_snapshot() must not TypeError."""
        import asyncio

        app, hub = _make_app()
        with (
            TestClient(app, raise_server_exceptions=False) as client,
            connect_test_ws(client, "/ws/worker/w1/term?token=x") as worker,
        ):
            _read_worker_snapshot_req(worker)
            worker.send_json({"type": "snapshot", "screen": "hi", "cols": 80, "rows": 25, "ts": None})
            snap = asyncio.run(hub.get_last_snapshot("w1"))
            if snap is not None:
                assert isinstance(snap.get("ts"), float), "ts must be float, not None"

    def test_negative_cols_falls_back_to_default(self) -> None:
        """cols=-10 is invalid; stored snapshot must use the safe default (80)."""
        import asyncio

        app, hub = _make_app()
        with (
            TestClient(app, raise_server_exceptions=False) as client,
            connect_test_ws(client, "/ws/worker/w1/term?token=x") as worker,
        ):
            _read_worker_snapshot_req(worker)
            worker.send_json({"type": "snapshot", "screen": "hi", "cols": -10, "rows": 25, "ts": time.time()})
            snap = asyncio.run(hub.get_last_snapshot("w1"))
            if snap is not None:
                assert snap.get("cols", 80) >= 1, "cols must be ≥ 1"

    def test_zero_rows_falls_back_to_default(self) -> None:
        """rows=0 is invalid; stored snapshot must use the safe default (25)."""
        import asyncio

        app, hub = _make_app()
        with (
            TestClient(app, raise_server_exceptions=False) as client,
            connect_test_ws(client, "/ws/worker/w1/term?token=x") as worker,
        ):
            _read_worker_snapshot_req(worker)
            worker.send_json({"type": "snapshot", "screen": "hi", "cols": 80, "rows": 0, "ts": time.time()})
            snap = asyncio.run(hub.get_last_snapshot("w1"))
            if snap is not None:
                assert snap.get("rows", 25) >= 1, "rows must be ≥ 1"
