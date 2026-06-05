#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for HijackClient against a real FastAPI app via ASGI transport."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport
from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

from provide.uterm.client.hijack import HijackClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WID = "test-worker"


def _make_hub_app() -> tuple[TermHub, FastAPI]:
    hub = TermHub(resolve_browser_role=lambda _ws, _wid: "admin")
    app = FastAPI()
    app.include_router(hub.create_router())
    return hub, app


def _add_worker(hub: TermHub, worker_id: str = WID) -> AsyncMock:
    mock_ws = AsyncMock()
    mock_ws.send_text = AsyncMock()
    hub.registry._workers[worker_id] = WorkerTermState(worker_ws=mock_ws)
    return mock_ws


def _client_for(app: FastAPI, **kwargs: object) -> HijackClient:
    return HijackClient(
        "http://test",
        transport=ASGITransport(app=app),
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Acquire / release lifecycle
# ---------------------------------------------------------------------------


class TestAcquireRelease:
    async def test_acquire_and_release(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, data = await c.acquire(WID, owner="tester", lease_s=60)

        assert ok is True
        assert data["ok"] is True
        assert "hijack_id" in data
        assert data["owner"] == "tester"

        async with _client_for(app) as c:
            ok, rel = await c.release(WID, data["hijack_id"])

        assert ok is True
        assert rel["ok"] is True

    async def test_acquire_no_worker(self) -> None:
        hub, app = _make_hub_app()

        async with _client_for(app) as c:
            ok, data = await c.acquire(WID)

        assert ok is False
        assert "error" in data

    async def test_acquire_conflict(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        hub.registry._workers[WID].hijack_session = HijackSession(
            hijack_id="existing",
            owner="other",
            acquired_at=time.time(),
            lease_expires_at=time.time() + 3600,
            last_heartbeat=time.time(),
        )

        async with _client_for(app) as c:
            ok, data = await c.acquire(WID)

        assert ok is False


# ---------------------------------------------------------------------------
# Send / snapshot / events / heartbeat / step
# ---------------------------------------------------------------------------


class TestHijackOperations:
    async def test_send_keys(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, acq = await c.acquire(WID)
            assert ok
            hid = acq["hijack_id"]

            ok, sent = await c.send(WID, hid, keys="hello\r")

        assert ok is True
        assert sent["sent"] == "hello\r"

    async def test_snapshot(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, acq = await c.acquire(WID)
            hid = acq["hijack_id"]

            ok, snap = await c.snapshot(WID, hid, wait_ms=50)

        assert ok is True
        assert "snapshot" in snap

    async def test_events(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, acq = await c.acquire(WID)
            hid = acq["hijack_id"]

            ok, ev = await c.events(WID, hid, after_seq=0, limit=10)

        assert ok is True
        assert "events" in ev

    async def test_heartbeat(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, acq = await c.acquire(WID)
            hid = acq["hijack_id"]

            ok, hb = await c.heartbeat(WID, hid, lease_s=120)

        assert ok is True
        assert hb["ok"] is True
        assert "lease_expires_at" in hb

    async def test_step(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, acq = await c.acquire(WID)
            hid = acq["hijack_id"]

            ok, st = await c.step(WID, hid)

        assert ok is True
        assert st["ok"] is True

    async def test_send_bad_hijack_id(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, data = await c.send(WID, "bad-id", keys="x")

        assert ok is False

    async def test_release_bad_hijack_id(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, data = await c.release(WID, "bad-id")

        assert ok is False


# ---------------------------------------------------------------------------
# Worker control
# ---------------------------------------------------------------------------


class TestWorkerControl:
    async def test_set_input_mode(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, data = await c.set_input_mode(WID, "open")

        assert ok is True
        assert data["input_mode"] == "open"

    async def test_set_input_mode_no_worker(self) -> None:
        hub, app = _make_hub_app()

        async with _client_for(app) as c:
            ok, data = await c.set_input_mode(WID, "open")

        assert ok is False

    async def test_disconnect_worker(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, data = await c.disconnect_worker(WID)

        assert ok is True
        assert data["ok"] is True

    async def test_disconnect_no_worker(self) -> None:
        hub, app = _make_hub_app()

        async with _client_for(app) as c:
            ok, data = await c.disconnect_worker(WID)

        assert ok is False


# (TestEntityPrefix, TestCustomHeaders, TestClientLifecycle, TestFullLifecycle,
#  TestSendGuards, TestSessionAPI, TestNonJsonResponse, TestEdgeCases,
#  TestTransportErrors moved to test_hijack_client_2.py)


class TestSanitize:
    """Unit tests for the `_sanitize` helper that scrubs log payloads."""

    def test_long_list_truncated_to_first_10(self) -> None:
        from provide.uterm.client.hijack import _sanitize

        result = _sanitize(list(range(25)))
        assert result == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, "..."]

    def test_long_string_truncated_with_ellipsis(self) -> None:
        from provide.uterm.client.hijack import _sanitize

        result = _sanitize("a" * 600)
        assert result == ("a" * 500) + "..."
        assert len(result) == 503

    def test_composite_keys_are_redacted(self) -> None:
        """Composite keys containing a sensitive token substring must be redacted."""
        from provide.uterm.client.hijack import _sanitize

        result = _sanitize({"api_key": "should-be-hidden", "access_token": "also-hidden"})
        assert result == {"api_key": "***", "access_token": "***"}

    def test_nested_composite_keys_are_redacted(self) -> None:
        """Nested dicts with composite sensitive keys must also be redacted."""
        from provide.uterm.client.hijack import _sanitize

        result = _sanitize({"credentials": {"api_key": "hidden", "access_token": "hidden"}})
        assert result["credentials"] == {"api_key": "***", "access_token": "***"}

    def test_non_sensitive_key_is_not_redacted(self) -> None:
        """Non-sensitive keys must pass through unchanged."""
        from provide.uterm.client.hijack import _sanitize

        result = _sanitize({"status": "ok", "count": 3})
        assert result == {"status": "ok", "count": 3}
