#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Coverage gap tests for routes/rest.py, routes/websockets.py, routes/browser_handlers.py — part 1.

Covers REST route edge cases: rate limiting, empty keys, and hijack release flows.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.terminal.bridge.hub import TermHub
from provide.terminal.bridge.models import HijackSession, WorkerTermState


def _make_app(**hub_kwargs: Any) -> tuple[TermHub, FastAPI, TestClient]:
    hub = TermHub(**hub_kwargs)
    app = FastAPI()
    app.include_router(hub.create_router())
    client = TestClient(app, raise_server_exceptions=True)
    return hub, app, client


def _read_initial(browser: Any) -> tuple[dict, dict]:
    """Read hello + hijack_state from a newly-connected browser WS."""
    hello = browser.receive_json()
    assert hello["type"] == "hello"
    hs = browser.receive_json()
    assert hs["type"] == "hijack_state"
    return hello, hs


# ---------------------------------------------------------------------------
# routes/rest.py lines 109-110 — rate-limited acquire (429)
# ---------------------------------------------------------------------------


class TestRestAcquireRateLimited:
    def test_acquire_rate_limited_returns_429(self) -> None:
        """Lines 109-110: allow_rest_acquire_for returns False → 429."""
        hub, app, client = _make_app()

        with patch.object(hub, "allow_rest_acquire_for", return_value=False):
            resp = client.post("/worker/w1/hijack/acquire", json={})
        assert resp.status_code == 429
        assert resp.json()["error"] == "rate_limited"


# ---------------------------------------------------------------------------
# routes/rest.py lines 308-311 — rate-limited send (429)
# ---------------------------------------------------------------------------


class TestRestSendRateLimited:
    def test_send_rate_limited_returns_429(self) -> None:
        """Lines 308-311: allow_rest_send_for returns False → 429."""
        hub, app, client = _make_app()

        with patch.object(hub, "allow_rest_send_for", return_value=False):
            resp = client.post(
                "/worker/w1/hijack/abcdef12-0000-0000-0000-000000000000/send",
                json={"keys": "hello"},
            )
        assert resp.status_code == 429
        assert resp.json()["error"] == "rate_limited"


# ---------------------------------------------------------------------------
# routes/rest.py line 318 — empty keys in hijack_send (400)
# ---------------------------------------------------------------------------


class TestRestSendEmptyKeys:
    def test_send_empty_keys_returns_400(self) -> None:
        """Line 318: request.keys is empty → 400."""
        import asyncio

        hub, app, client = _make_app()
        now = time.time()
        hid = "abcdef12-0000-0000-0000-000000000000"

        # Register a valid REST session
        async def _setup() -> None:
            async with hub._lock:
                st = hub._workers.setdefault("w1", WorkerTermState())
                st.worker_ws = AsyncMock()
                st.worker_ws.send_text = AsyncMock()
                st.hijack_session = HijackSession(
                    hijack_id=hid,
                    owner="tester",
                    acquired_at=now,
                    lease_expires_at=now + 300,
                    last_heartbeat=now,
                )

        asyncio.run(_setup())

        resp = client.post(f"/worker/w1/hijack/{hid}/send", json={"keys": ""})
        assert resp.status_code == 400
        assert "keys must not be empty" in resp.json()["error"]


# ---------------------------------------------------------------------------
# routes/rest.py lines 386-389 — rate-limited step (429)
# ---------------------------------------------------------------------------


class TestRestStepRateLimited:
    def test_step_rate_limited_returns_429(self) -> None:
        """Lines 386-389: allow_rest_send_for returns False → 429."""
        hub, app, client = _make_app()

        with patch.object(hub, "allow_rest_send_for", return_value=False):
            resp = client.post(
                "/worker/w1/hijack/abcdef12-0000-0000-0000-000000000000/step",
            )
        assert resp.status_code == 429
        assert resp.json()["error"] == "rate_limited"


# ---------------------------------------------------------------------------
# routes/rest.py line 423, 424->429 — hijack_release should_resume
# ---------------------------------------------------------------------------


class TestRestHijackRelease:
    def test_release_should_resume_true_sends_resume(self) -> None:
        """Lines 424->429: should_resume=True → send resume + notify."""
        import asyncio

        hub, app, client = _make_app()
        now = time.time()
        hid = "abcdef12-0000-0000-0000-000000000000"

        notify_calls: list[dict] = []
        original_notify = hub.notify_hijack_changed

        def _capture_notify(wid: str, *, enabled: bool, owner: Any = None) -> None:
            notify_calls.append({"worker_id": wid, "enabled": enabled, "owner": owner})
            return original_notify(wid, enabled=enabled, owner=owner)

        hub.notify_hijack_changed = _capture_notify  # type: ignore[method-assign]

        async def _setup() -> None:
            async with hub._lock:
                st = hub._workers.setdefault("w1", WorkerTermState())
                st.worker_ws = AsyncMock()
                st.worker_ws.send_text = AsyncMock()
                st.hijack_session = HijackSession(
                    hijack_id=hid,
                    owner="tester",
                    acquired_at=now,
                    lease_expires_at=now + 300,
                    last_heartbeat=now,
                )

        asyncio.run(_setup())

        resp = client.post(f"/worker/w1/hijack/{hid}/release")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # notify_hijack_changed should have been called with enabled=False
        notify_calls_off = [c for c in notify_calls if not c["enabled"]]
        assert len(notify_calls_off) >= 1

    def test_release_should_resume_false_when_recheck_finds_hijack(self) -> None:
        """Line 423: should_resume=False after re-check finds new hijack active."""
        import asyncio

        hub, app, client = _make_app()
        now = time.time()
        hid = "abcdef12-0000-0000-0000-000000000000"

        async def _setup() -> None:
            async with hub._lock:
                st = hub._workers.setdefault("w1", WorkerTermState())
                st.worker_ws = AsyncMock()
                st.worker_ws.send_text = AsyncMock()
                st.hijack_session = HijackSession(
                    hijack_id=hid,
                    owner="tester",
                    acquired_at=now,
                    lease_expires_at=now + 300,
                    last_heartbeat=now,
                )

        asyncio.run(_setup())

        # Patch check_still_hijacked to return True (simulates new hijack between release and recheck)
        with patch.object(hub, "check_still_hijacked", new=AsyncMock(return_value=True)):
            resp = client.post(f"/worker/w1/hijack/{hid}/release")

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
