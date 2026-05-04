#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Disconnect-worker and TOCTOU re-check coverage tests (split from test_coverage_hub_routes.py)."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from provide.terminal.bridge.hub import TermHub


def _make_app() -> tuple[TermHub, FastAPI]:
    hub = TermHub()
    app = FastAPI()
    app.include_router(hub.create_router())
    return hub, app


# ---------------------------------------------------------------------------
# hub._disconnect_worker: ws.close() raises (lines 382-383)
# ---------------------------------------------------------------------------


class TestDisconnectWorkerCloseError:
    """hub:382-383 — ws.close() raises an exception inside _disconnect_worker."""

    async def test_ws_close_error_handled(self) -> None:
        from provide.terminal.bridge.models import WorkerTermState

        hub, app = _make_app()
        mock_ws = AsyncMock()
        mock_ws.close = AsyncMock(side_effect=RuntimeError("already closed"))
        mock_ws.send_text = AsyncMock()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = mock_ws

        ok = await hub.disconnect_worker("w1")
        assert ok is True
        mock_ws.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# hub._disconnect_worker: was_hijacked=True path (lines 389-390)
# ---------------------------------------------------------------------------


class TestDisconnectWorkerWasHijacked:
    """hub:389-390 — was_hijacked=True fires _notify_hijack_changed + broadcast."""

    async def test_disconnect_with_active_hijack_notifies(self) -> None:
        from provide.terminal.bridge.models import HijackSession, WorkerTermState

        hijack_calls: list[dict[str, Any]] = []

        def _on_change(wid: str, enabled: bool, owner: str | None) -> None:
            hijack_calls.append({"worker_id": wid, "enabled": enabled, "owner": owner})

        hub = TermHub(on_hijack_changed=_on_change)
        app = FastAPI()
        app.include_router(hub.create_router())

        mock_ws = AsyncMock()
        mock_ws.close = AsyncMock()
        mock_ws.send_text = AsyncMock()

        now = time.monotonic()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = mock_ws
            st.hijack_session = HijackSession(
                hijack_id="test-hid",
                owner="tester",
                acquired_at=now,
                lease_expires_at=now + 300,
                last_heartbeat=now,
            )

        ok = await hub.disconnect_worker("w1")
        assert ok is True
        assert len(hijack_calls) == 1
        assert hijack_calls[0]["enabled"] is False


# ---------------------------------------------------------------------------
# routes_rest: send TOCTOU re-check path (line 312)
# ---------------------------------------------------------------------------


class TestRestSendToctouRecheck:
    """The send endpoint re-validates the session under lock (line 312);
    if it expired between _wait_for_guard and the re-check, it returns 404."""

    async def test_send_returns_404_when_session_expires_during_guard(self) -> None:
        from unittest.mock import patch

        from provide.terminal.bridge.models import HijackSession, WorkerTermState

        hub, app = _make_app()
        hid = "aabb0011-2233-4455-6677-000000000001"
        now = time.monotonic()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = AsyncMock()
            st.worker_ws.send_text = AsyncMock()
            st.hijack_session = HijackSession(
                hijack_id=hid,
                owner="tester",
                acquired_at=now,
                lease_expires_at=now + 600,
                last_heartbeat=now,
            )

        async def _guard_that_expires(*args, **kwargs):
            async with hub._lock:
                st2 = hub._workers.get("w1")
                if st2 is not None:
                    st2.hijack_session = None  # simulate expiry
            return True, {"screen": ""}, None

        with patch.object(hub, "wait_for_guard", side_effect=_guard_that_expires):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(f"/worker/w1/hijack/{hid}/send", json={"keys": "x"})
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# routes_rest: step TOCTOU re-check path (line 367)
# ---------------------------------------------------------------------------


class TestRestStepToctouRecheck:
    """Step re-validates the session under lock (line 368);
    if it expired between _get_rest_session and the re-check, it returns 404."""

    async def test_step_returns_404_when_session_expires_between_checks(self) -> None:
        from unittest.mock import patch

        from provide.terminal.bridge.models import HijackSession, WorkerTermState

        hub, app = _make_app()
        hid = "aabb0011-2233-4455-6677-000000000002"
        now = time.monotonic()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = AsyncMock()
            st.worker_ws.send_text = AsyncMock()
            st.hijack_session = HijackSession(
                hijack_id=hid,
                owner="tester",
                acquired_at=now,
                lease_expires_at=now + 600,
                last_heartbeat=now,
            )

        _original = hub.get_rest_session

        async def _get_then_expire(worker_id, hijack_id):
            result = await _original(worker_id, hijack_id)
            if result is not None:
                async with hub._lock:
                    st2 = hub._workers.get(worker_id)
                    if st2 is not None:
                        st2.hijack_session = None
            return result

        with patch.object(hub, "get_rest_session", side_effect=_get_then_expire):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(f"/worker/w1/hijack/{hid}/step")
        assert r.status_code == 404
