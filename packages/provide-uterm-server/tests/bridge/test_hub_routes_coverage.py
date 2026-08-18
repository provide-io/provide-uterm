#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Targeted tests to cover hub.py, routes_rest.py, and routes_ws.py edge paths."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

from provide.uterm.server.bridge.hub import TermHub

from .control_channel_helpers import decode_control_payload, decode_control_payloads


def _make_app() -> tuple[TermHub, FastAPI]:
    hub = TermHub()
    app = FastAPI()
    app.include_router(hub.create_router())
    return hub, app


def _ws_msg(data: dict[str, Any]) -> str:
    return json.dumps(data)


# ---------------------------------------------------------------------------
# hub._wait_for_snapshot
# ---------------------------------------------------------------------------


class TestWaitForSnapshot:
    async def test_returns_none_when_worker_missing(self) -> None:
        """_wait_for_snapshot returns None if worker disappears mid-wait."""
        hub, _ = _make_app()
        # No worker registered → returns None immediately
        result = await hub.wait_for_snapshot("nonexistent", timeout_ms=100)
        assert result is None

    async def test_returns_fresh_snapshot(self) -> None:
        """_wait_for_snapshot returns a snapshot with ts > request time."""
        hub, _ = _make_app()
        async with hub._lock:
            from provide.uterm.server.bridge.models import WorkerTermState

            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = AsyncMock()
            st.worker_ws.send_text = AsyncMock()

        # Set a fresh snapshot after a short delay
        async def _set_snapshot():
            await asyncio.sleep(0.05)
            async with hub._lock:
                st2 = hub.registry._workers["w1"]
                st2.last_snapshot = {"type": "snapshot", "screen": "test", "ts": time.time()}

        task = asyncio.create_task(_set_snapshot())
        result = await hub.wait_for_snapshot("w1", timeout_ms=2000)
        await task
        assert result is not None
        assert result["screen"] == "test"


# ---------------------------------------------------------------------------
# hub._touch_hijack_owner
# ---------------------------------------------------------------------------


class TestTouchHijackOwner:
    async def test_returns_none_when_no_worker(self) -> None:
        hub, _ = _make_app()
        result = await hub.touch_hijack_owner("nonexistent")
        assert result is None

    async def test_returns_none_when_no_owner(self) -> None:
        hub, _ = _make_app()
        async with hub._lock:
            from provide.uterm.server.bridge.models import WorkerTermState

            hub.registry._workers["w1"] = WorkerTermState()
        result = await hub.touch_hijack_owner("w1")
        assert result is None


# ---------------------------------------------------------------------------
# routes_rest session validation (404 paths)
# ---------------------------------------------------------------------------


class TestRestSessionValidation:
    """Tests for expired/invalid hijack session responses on REST routes."""

    async def test_snapshot_returns_404_for_bad_hijack_id(self) -> None:
        hub, app = _make_app()
        async with hub._lock:
            from provide.uterm.server.bridge.models import WorkerTermState

            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = AsyncMock()
            st.worker_ws.send_text = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/worker/w1/hijack/dead0000-0000-0000-0000-000000000000/snapshot")
        assert r.status_code == 404

    async def test_events_returns_404_for_bad_hijack_id(self) -> None:
        hub, app = _make_app()
        async with hub._lock:
            from provide.uterm.server.bridge.models import WorkerTermState

            hub.registry._workers.setdefault("w1", WorkerTermState())

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/worker/w1/hijack/dead0000-0000-0000-0000-000000000000/events")
        assert r.status_code == 404

    async def test_send_returns_404_for_bad_hijack_id(self) -> None:
        hub, app = _make_app()
        async with hub._lock:
            from provide.uterm.server.bridge.models import WorkerTermState

            hub.registry._workers.setdefault("w1", WorkerTermState())

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/worker/w1/hijack/dead0000-0000-0000-0000-000000000000/send",
                json={"keys": "x"},
            )
        assert r.status_code == 404

    async def test_step_returns_404_for_bad_hijack_id(self) -> None:
        hub, app = _make_app()
        async with hub._lock:
            from provide.uterm.server.bridge.models import WorkerTermState

            hub.registry._workers.setdefault("w1", WorkerTermState())

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/worker/w1/hijack/dead0000-0000-0000-0000-000000000000/step")
        assert r.status_code == 404

    async def test_step_returns_404_for_expired_session(self) -> None:
        """Step re-validation fails when session expired between get and re-check."""
        hub, app = _make_app()
        # Set up worker with a REST session that we can expire
        async with hub._lock:
            from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = AsyncMock()
            st.worker_ws.send_text = AsyncMock()
            # Create a session that expires immediately
            now = time.monotonic()
            st.hijack_session = HijackSession(
                hijack_id="aabb0011-2233-4455-6677-889900aabbcc",
                owner="tester",
                acquired_at=now - 10,
                lease_expires_at=now - 1,  # already expired
                last_heartbeat=now - 10,
            )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/worker/w1/hijack/aabb0011-2233-4455-6677-889900aabbcc/step")
        # _get_rest_session checks expiry → 404
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# routes_ws._safe_int
# ---------------------------------------------------------------------------


class TestRoutesWsSafeInt:
    async def test_safe_int_non_numeric(self) -> None:
        from provide.uterm.server.bridge.routes.websockets import _safe_int

        assert _safe_int("not_a_number", 80) == 80

    async def test_safe_int_object(self) -> None:
        from provide.uterm.server.bridge.routes.websockets import _safe_int

        assert _safe_int(object(), 25) == 25


# ---------------------------------------------------------------------------
# websocket.py — buffered data on disconnect
# ---------------------------------------------------------------------------


class TestWebSocketStreamReaderBufferedDisconnect:
    async def test_disconnect_returns_buffered_data(self) -> None:
        """When WS disconnects with data in buffer, read() returns remaining bytes."""
        from starlette.websockets import WebSocketDisconnect

        from provide.uterm.transports.websocket import WebSocketStreamReader

        ws = AsyncMock()
        call_count = 0

        async def _recv_text():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "hello"  # first call returns data
            raise WebSocketDisconnect(code=1000)  # second call disconnects

        ws.receive_text = _recv_text

        reader = WebSocketStreamReader(ws)
        # Request more bytes than "hello" provides → triggers second recv → disconnect
        data = await reader.read(100)
        assert data == b"hello"

        # Next read returns empty (closed)
        data2 = await reader.read(100)
        assert data2 == b""


# ---------------------------------------------------------------------------
# mount_terminal_ui
# ---------------------------------------------------------------------------


class TestMountTerminalUi:
    async def test_mount_registers_route(self) -> None:
        """mount_terminal_ui registers a static file mount on the app."""
        from provide.uterm.fastapi_utils import mount_terminal_ui

        app = FastAPI()
        mount_terminal_ui(app)
        # Verify the mount was added
        route_names = [getattr(r, "name", None) for r in app.routes]
        assert "terminal-ui" in route_names


# ---------------------------------------------------------------------------
# hub._broadcast dead-socket cleanup with hijack owner  (lines 285-300)
# ---------------------------------------------------------------------------


class TestBroadcastDeadSocketHijackOwner:
    """When a browser WS that is the hijack_owner dies during _broadcast,
    the hub must clear ownership and send 'resume' to the worker."""

    async def test_dead_hijack_owner_triggers_resume(self) -> None:
        from provide.uterm.server.bridge.models import WorkerTermState

        hijack_changed_calls: list[dict[str, Any]] = []

        def _on_change(wid: str, enabled: bool, owner: str | None) -> None:
            hijack_changed_calls.append({"worker_id": wid, "enabled": enabled, "owner": owner})

        hub = TermHub(on_hijack_changed=_on_change)

        dead_browser = AsyncMock()
        dead_browser.send_text = AsyncMock(side_effect=RuntimeError("connection lost"))

        worker_ws = AsyncMock()
        sent_to_worker: list[str] = []
        worker_ws.send_text = AsyncMock(side_effect=lambda p: sent_to_worker.append(p))

        async with hub._lock:
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[dead_browser] = "operator"
            st.hijack_owner = dead_browser
            st.hijack_owner_expires_at = time.monotonic() + 600

        await hub.broadcast("w1", {"type": "test_msg"})

        # Worker should have received the original broadcast + resume control
        decoded = decode_control_payloads(sent_to_worker)
        resume_msgs = [m for m in decoded if m.get("action") == "resume"]
        assert len(resume_msgs) == 1
        assert resume_msgs[0]["action"] == "resume"

        # on_hijack_changed should have fired with enabled=False
        assert len(hijack_changed_calls) == 1
        assert hijack_changed_calls[0]["enabled"] is False

        # Browser should be removed from set
        async with hub._lock:
            st2 = hub.registry._workers["w1"]
            assert dead_browser not in st2.browsers
            assert st2.hijack_owner is None


# ---------------------------------------------------------------------------
# hub._broadcast_hijack_state dead-socket cleanup with hijack owner (lines 356-371)
# ---------------------------------------------------------------------------


class TestBroadcastHijackStateDeadSocketOwner:
    """Same dead-socket-with-owner path, but triggered via _broadcast_hijack_state."""

    async def test_dead_hijack_owner_in_broadcast_hijack_state(self) -> None:
        from provide.uterm.server.bridge.models import WorkerTermState

        hub, _ = _make_app()

        dead_browser = AsyncMock()
        dead_browser.send_text = AsyncMock(side_effect=RuntimeError("connection lost"))

        worker_ws = AsyncMock()
        sent_to_worker: list[str] = []
        worker_ws.send_text = AsyncMock(side_effect=lambda p: sent_to_worker.append(p))

        async with hub._lock:
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[dead_browser] = "operator"
            st.hijack_owner = dead_browser
            st.hijack_owner_expires_at = time.monotonic() + 600

        await hub.broadcast_hijack_state("w1")

        # Resume should have been sent to the worker
        resume_msgs = [decode_control_payload(p) for p in sent_to_worker if "resume" in p]
        assert len(resume_msgs) == 1
        assert resume_msgs[0]["action"] == "resume"


# ---------------------------------------------------------------------------
# hub._try_acquire_rest_hijack no_worker path (line 433)
# ---------------------------------------------------------------------------


class TestTryAcquireRestHijackNoWorker:
    async def test_returns_no_worker_when_disconnected(self) -> None:
        hub, _ = _make_app()
        ok, err = await hub.try_acquire_rest_hijack(
            "no-such-worker", owner="owner", lease_s=300, hijack_id="aabb", now=time.monotonic()
        )
        assert ok is False
        assert err == "no_worker"

    async def test_returns_no_worker_when_ws_is_none(self) -> None:
        from provide.uterm.server.bridge.models import WorkerTermState

        hub, _ = _make_app()
        async with hub._lock:
            hub.registry._workers["w1"] = WorkerTermState()  # worker_ws defaults to None
        ok, err = await hub.try_acquire_rest_hijack(
            "w1", owner="owner", lease_s=300, hijack_id="aabb", now=time.monotonic()
        )
        assert ok is False
        assert err == "no_worker"


# ---------------------------------------------------------------------------
# hub._touch_hijack_owner with custom lease_s (lines 469-471)
# ---------------------------------------------------------------------------


class TestTouchHijackOwnerWithLease:
    async def test_custom_lease_s(self) -> None:
        from provide.uterm.server.bridge.models import WorkerTermState

        hub, _ = _make_app()
        mock_ws = AsyncMock()
        async with hub._lock:
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.hijack_owner = mock_ws
            st.hijack_owner_expires_at = time.monotonic() + 10

        result = await hub.touch_hijack_owner("w1", lease_s=120)
        assert result is not None
        # Should be approximately now + 120
        assert result > time.monotonic() + 100


# ---------------------------------------------------------------------------
# allow_rest_acquire_for: LRU-lite eviction on cache overflow
# ---------------------------------------------------------------------------


class TestAllowRestAcquireForLruEviction:
    """connections.py — on cache overflow, oldest half is evicted (not all)."""

    async def test_evicts_oldest_half_not_all(self) -> None:
        from provide.uterm.server.bridge.hub.connections import _REST_CLIENT_CACHE_MAX, _REST_CLIENT_EVICT_COUNT

        hub, _ = _make_app()

        # Fill the cache to the limit using distinct client IDs.
        for i in range(_REST_CLIENT_CACHE_MAX):
            hub.limiter.rest_acquire_per_client[f"client-{i}"] = hub.limiter.rest_acquire_bucket

        assert len(hub.limiter.rest_acquire_per_client) == _REST_CLIENT_CACHE_MAX

        # One more call triggers eviction.
        hub.allow_rest_acquire_for("new-client")

        # After eviction the cache should contain (MAX - EVICT_COUNT) + 1 entries,
        # NOT be fully cleared.
        remaining = len(hub.limiter.rest_acquire_per_client)
        assert remaining == _REST_CLIENT_CACHE_MAX - _REST_CLIENT_EVICT_COUNT + 1
        # The newest client must be present; the oldest should be gone.
        assert "new-client" in hub.limiter.rest_acquire_per_client
        assert "client-0" not in hub.limiter.rest_acquire_per_client
        # A recently-added client near the end should survive.
        assert f"client-{_REST_CLIENT_CACHE_MAX - 1}" in hub.limiter.rest_acquire_per_client


# disconnect_worker and TOCTOU re-check tests moved to
# test_coverage_hub_routes_p2.py to keep this file under 500 LOC.
