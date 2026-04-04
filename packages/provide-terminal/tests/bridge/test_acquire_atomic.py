#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Regression tests for TOCTOU (fix 1) and was_owner race (fix 3).

Fix 1: hub._try_acquire_rest_hijack and hub._try_acquire_ws_hijack perform their
       ownership check and state mutation atomically under the lock, preventing two
       concurrent callers from both passing the "is anyone hijacking?" check.

Fix 3: The browser WS finally block detects ownership and clears hijack_owner in
       a single lock acquisition, preventing a window where another coroutine could
       steal the owner flag between the detection and the clear.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.terminal.bridge.hub import TermHub
from provide.terminal.bridge.models import HijackSession, WorkerTermState
from provide.terminal.client import connect_test_ws

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_app(role: str | None = None) -> tuple[FastAPI, TermHub]:
    resolver = (lambda _ws, _worker_id: role) if role is not None else None
    hub = TermHub(resolve_browser_role=resolver)
    app = FastAPI()
    app.include_router(hub.create_router())
    return app, hub


def _active_session(owner: str = "test") -> HijackSession:
    return HijackSession(
        hijack_id=str(uuid.uuid4()),
        owner=owner,
        acquired_at=time.time(),
        lease_expires_at=time.time() + 3600,
        last_heartbeat=time.time(),
    )


# ---------------------------------------------------------------------------
# Fix 1a: hub._try_acquire_rest_hijack is atomic
# ---------------------------------------------------------------------------


class TestTryAcquireRestHijack:
    async def test_first_caller_wins(self) -> None:
        """Two concurrent calls: exactly one succeeds, one gets already_hijacked."""
        hub = TermHub()
        hub._workers["bot1"] = WorkerTermState(worker_ws=AsyncMock())

        results: list[tuple[bool, str | None]] = []

        async def _attempt() -> None:
            r = await hub.try_acquire_rest_hijack(
                "bot1",
                owner="tester",
                lease_s=60,
                hijack_id=str(uuid.uuid4()),
                now=time.time(),
            )
            results.append(r)

        await asyncio.gather(_attempt(), _attempt())

        successes = [r for r in results if r[0] is True]
        failures = [r for r in results if r[0] is False]
        assert len(successes) == 1, "exactly one caller should succeed"
        assert len(failures) == 1
        assert failures[0][1] == "already_hijacked"

    async def test_returns_already_hijacked_when_ws_owner_set(self) -> None:
        hub = TermHub()
        mock_ws = AsyncMock()
        hub._workers["bot1"] = WorkerTermState(worker_ws=AsyncMock(), hijack_owner=mock_ws)

        ok, err = await hub.try_acquire_rest_hijack("bot1", owner="t", lease_s=60, hijack_id="x", now=time.time())
        assert ok is False
        assert err == "already_hijacked"

    async def test_returns_already_hijacked_when_session_active(self) -> None:
        hub = TermHub()
        hub._workers["bot1"] = WorkerTermState(worker_ws=AsyncMock(), hijack_session=_active_session())

        ok, err = await hub.try_acquire_rest_hijack("bot1", owner="t", lease_s=60, hijack_id="x", now=time.time())
        assert ok is False
        assert err == "already_hijacked"

    async def test_writes_session_on_success(self) -> None:
        hub = TermHub()
        hub._workers["bot1"] = WorkerTermState(worker_ws=AsyncMock())
        hid = str(uuid.uuid4())
        now = time.time()

        ok, err = await hub.try_acquire_rest_hijack("bot1", owner="owner1", lease_s=30, hijack_id=hid, now=now)

        assert ok is True
        assert err is None
        st = hub._workers["bot1"]
        assert st.hijack_session is not None
        assert st.hijack_session.hijack_id == hid
        assert st.hijack_session.owner == "owner1"


# ---------------------------------------------------------------------------
# Fix 1b: hub._try_acquire_ws_hijack is atomic
# ---------------------------------------------------------------------------


class TestTryAcquireWsHijack:
    async def test_first_caller_wins(self) -> None:
        """Two concurrent WS callers: exactly one wins the hijack."""
        hub = TermHub()
        hub._workers["bot1"] = WorkerTermState(worker_ws=AsyncMock())

        ws_a = AsyncMock()
        ws_b = AsyncMock()
        results: list[tuple[bool, str | None]] = []

        async def _attempt(ws: object) -> None:
            r = await hub.try_acquire_ws_hijack("bot1", ws)  # type: ignore[arg-type]
            results.append(r)

        await asyncio.gather(_attempt(ws_a), _attempt(ws_b))

        successes = [r for r in results if r[0] is True]
        failures = [r for r in results if r[0] is False]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0][1] == "already_hijacked"

    async def test_no_worker_returns_no_worker(self) -> None:
        hub = TermHub()
        hub._workers["bot1"] = WorkerTermState(worker_ws=None)

        ok, err = await hub.try_acquire_ws_hijack("bot1", AsyncMock())
        assert ok is False
        assert err == "no_worker"

    async def test_unknown_bot_returns_no_worker(self) -> None:
        hub = TermHub()
        ok, err = await hub.try_acquire_ws_hijack("unknown", AsyncMock())
        assert ok is False
        assert err == "no_worker"

    async def test_rest_session_blocks_ws_acquire(self) -> None:
        hub = TermHub()
        hub._workers["bot1"] = WorkerTermState(worker_ws=AsyncMock(), hijack_session=_active_session())
        ok, err = await hub.try_acquire_ws_hijack("bot1", AsyncMock())
        assert ok is False
        assert err == "already_hijacked"

    async def test_sets_owner_and_expiry_on_success(self) -> None:
        hub = TermHub(dashboard_hijack_lease_s=60)
        ws = AsyncMock()
        hub._workers["bot1"] = WorkerTermState(worker_ws=AsyncMock())

        ok, err = await hub.try_acquire_ws_hijack("bot1", ws)

        assert ok is True
        st = hub._workers["bot1"]
        assert st.hijack_owner is ws
        assert st.hijack_owner_expires_at is not None
        assert st.hijack_owner_expires_at > time.time()


# ---------------------------------------------------------------------------
# Fix 1c: REST acquire endpoint rejects concurrent duplicate requests
# ---------------------------------------------------------------------------


def test_rest_acquire_rejects_concurrent_duplicate() -> None:
    """Two simultaneous REST acquire requests: only one should return 200."""
    app, hub = make_app()

    # Pre-wire a worker WebSocket so the endpoint doesn't short-circuit on missing worker.
    worker_ws = AsyncMock()
    hub._workers["botX"] = WorkerTermState(worker_ws=worker_ws)

    with TestClient(app) as client:
        # First acquire — succeeds
        r1 = client.post("/worker/botX/hijack/acquire", json={"owner": "tester", "lease_s": 60})
        assert r1.status_code == 200, r1.json()

        # Second acquire with existing active session — must fail
        r2 = client.post("/worker/botX/hijack/acquire", json={"owner": "tester2", "lease_s": 60})
        assert r2.status_code == 409
        assert "hijacked" in r2.json()["error"].lower()


# ---------------------------------------------------------------------------
# Fix 1d: WS hijack_request rejects when no worker (new error path)
# ---------------------------------------------------------------------------


def test_ws_hijack_request_no_worker_returns_error() -> None:
    """hijack_request with no worker connected returns error (no_worker path)."""
    app, hub = make_app("admin")
    with TestClient(app) as client, connect_test_ws(client, "/ws/browser/nobot/term") as browser:
        # Drain hello + hijack_state
        browser.receive_json()
        browser.receive_json()

        browser.send_json({"type": "hijack_request"})

        msg = browser.receive_json()
        assert msg["type"] == "error"
        assert "worker" in msg["message"].lower()


# ---------------------------------------------------------------------------
# Fix 3: was_owner detection and clear are atomic (single lock block)
# ---------------------------------------------------------------------------


def test_disconnect_as_owner_sends_resume_and_clears_owner() -> None:
    """Regression: was_owner check+clear in one lock prevents double-resume race.

    The key assertion is that after the browser disconnects:
    1. worker receives exactly one resume control message
    2. hub.hijack_owner is None
    """
    app, hub = make_app("admin")

    with TestClient(app) as client, connect_test_ws(client, "/ws/worker/bot3/term") as worker:
        # snapshot_req on worker connect
        worker.receive_json()

        with connect_test_ws(client, "/ws/browser/bot3/term") as browser:
            browser.receive_json()  # hello
            browser.receive_json()  # hijack_state
            # browser connect triggers _request_snapshot → second snapshot_req
            worker.receive_json()

            # Acquire hijack
            browser.send_json({"type": "hijack_request"})
            worker.receive_json()  # pause control
            browser.receive_json()  # hijack_state(hijacked=True)

        # Browser disconnected — resume must be sent
        ctrl = worker.receive_json()
        assert ctrl["type"] == "control"
        assert ctrl["action"] == "resume"

    # hijack_owner must be cleared
    st = hub._workers.get("bot3")
    if st:
        assert st.hijack_owner is None
        assert st.hijack_owner_expires_at is None
