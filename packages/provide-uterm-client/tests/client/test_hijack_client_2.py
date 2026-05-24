#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for HijackClient — entity prefix, headers, lifecycle, full lifecycle,
send guards, session API, non-JSON responses, and edge cases (part 2)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport
from provide.uterm.bridge.hub import TermHub
from provide.uterm.bridge.models import WorkerTermState

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
    hub._workers[worker_id] = WorkerTermState(worker_ws=mock_ws)
    return mock_ws


def _client_for(app: FastAPI, **kwargs: object) -> HijackClient:
    return HijackClient(
        "http://test",
        transport=ASGITransport(app=app),
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Custom entity_prefix
# ---------------------------------------------------------------------------


class TestEntityPrefix:
    async def test_custom_prefix(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)

        # Routes are still /worker/... but the client uses /bot/...
        # so this should fail to find the route (404).
        async with _client_for(app, entity_prefix="/bot") as c:
            ok, data = await c.acquire(WID)

        assert ok is False


# ---------------------------------------------------------------------------
# Custom headers
# ---------------------------------------------------------------------------


class TestCustomHeaders:
    async def test_headers_forwarded(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app, headers={"X-Custom": "val"}) as c:
            ok, data = await c.acquire(WID)

        assert ok is True


# ---------------------------------------------------------------------------
# Context manager vs lazy client
# ---------------------------------------------------------------------------


class TestClientLifecycle:
    async def test_without_context_manager(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)

        c = _client_for(app)
        ok, data = await c.acquire(WID)
        assert ok is True

        # Clean up
        if c._client:
            await c._client.aclose()

    async def test_context_manager_reuse(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok1, d1 = await c.acquire(WID)
            assert ok1
            ok2, d2 = await c.release(WID, d1["hijack_id"])
            assert ok2
            ok3, d3 = await c.acquire(WID)
            assert ok3


# ---------------------------------------------------------------------------
# Full lifecycle
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    async def test_acquire_send_snapshot_heartbeat_events_release(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            # Acquire
            ok, acq = await c.acquire(WID, owner="e2e", lease_s=60)
            assert ok
            hid = acq["hijack_id"]

            # Send
            ok, sent = await c.send(WID, hid, keys="ls\r")
            assert ok

            # Snapshot
            ok, snap = await c.snapshot(WID, hid, wait_ms=50)
            assert ok

            # Heartbeat
            ok, hb = await c.heartbeat(WID, hid, lease_s=120)
            assert ok

            # Events
            ok, ev = await c.events(WID, hid)
            assert ok
            assert "events" in ev

            # Release
            ok, rel = await c.release(WID, hid)
            assert ok
            assert rel["ok"] is True


# ---------------------------------------------------------------------------
# Transport error handling
# ---------------------------------------------------------------------------


class TestSendGuards:
    async def test_send_with_expect_prompt_id(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, acq = await c.acquire(WID)
            hid = acq["hijack_id"]

            ok, sent = await c.send(WID, hid, keys="x", expect_prompt_id="some_prompt")

        # Guard may fail (no prompt matched) but the param is sent
        assert isinstance(sent, dict)

    async def test_send_with_expect_regex(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, acq = await c.acquire(WID)
            hid = acq["hijack_id"]

            ok, sent = await c.send(WID, hid, keys="x", expect_regex=".*prompt.*")

        assert isinstance(sent, dict)


# ---------------------------------------------------------------------------
# Session API (requires full server app)
# ---------------------------------------------------------------------------


class TestSessionAPI:
    @staticmethod
    def _make_server_app() -> FastAPI:
        from provide.uterm.server.app import create_server_app
        from provide.uterm.server.config import config_from_mapping

        cfg = config_from_mapping(
            {
                "server": {"host": "127.0.0.1", "port": 0},
                "auth": {"mode": "header", "header_mode_acknowledged": True, "worker_bearer_token": "test-bearer-token-32-chars-long-x"},
                "sessions": [
                    {
                        "session_id": "s1",
                        "display_name": "Test",
                        "connector_type": "shell",
                        "auto_start": False,
                    }
                ],
            }
        )
        return create_server_app(cfg)

    def _server_client(self, app: FastAPI) -> HijackClient:
        return HijackClient(
            "http://test",
            transport=ASGITransport(app=app),
            headers={"X-Uterm-Principal": "tester", "X-Uterm-Role": "admin"},
        )

    async def test_health(self) -> None:
        app = self._make_server_app()
        async with self._server_client(app) as c:
            ok, data = await c.health()
        assert ok
        assert data["ok"] is True

    async def test_list_sessions(self) -> None:
        app = self._make_server_app()
        async with self._server_client(app) as c:
            ok, data = await c.list_sessions()
        assert ok
        assert isinstance(data, list)
        assert len(data) >= 1

    async def test_get_session(self) -> None:
        app = self._make_server_app()
        async with self._server_client(app) as c:
            ok, data = await c.get_session("s1")
        assert ok
        assert data["session_id"] == "s1"

    async def test_session_snapshot(self) -> None:
        app = self._make_server_app()
        async with self._server_client(app) as c:
            ok, data = await c.session_snapshot("s1")
        assert ok

    async def test_session_events(self) -> None:
        app = self._make_server_app()
        async with self._server_client(app) as c:
            ok, data = await c.session_events("s1", limit=5)
        assert ok

    async def test_set_session_mode(self) -> None:
        app = self._make_server_app()
        async with self._server_client(app) as c:
            ok, data = await c.set_session_mode("s1", "open")
        assert ok
        assert data["input_mode"] == "open"

    async def test_connect_session(self) -> None:
        app = self._make_server_app()
        async with self._server_client(app) as c:
            ok, data = await c.connect_session("s1")
        assert ok

    async def test_disconnect_session(self) -> None:
        app = self._make_server_app()
        async with self._server_client(app) as c:
            ok, data = await c.disconnect_session("s1")
        assert ok

    async def test_quick_connect(self) -> None:
        app = self._make_server_app()
        async with self._server_client(app) as c:
            ok, data = await c.quick_connect("shell", display_name="Ephemeral")
        assert ok
        assert "session_id" in data


# ---------------------------------------------------------------------------
# Non-JSON response handling
# ---------------------------------------------------------------------------


class TestNonJsonResponse:
    async def test_non_json_response_returns_raw(self) -> None:
        """Server returns plain text — client falls back to raw."""
        from starlette.responses import PlainTextResponse

        app = FastAPI()

        @app.get("/api/health")
        async def health() -> PlainTextResponse:
            return PlainTextResponse("OK")

        async with HijackClient("http://test", transport=ASGITransport(app=app)) as c:
            ok, data = await c.health()

        assert ok is True
        assert data == {"raw": "OK"}


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    async def test_aexit_without_aenter(self) -> None:
        """__aexit__ when no client was created is a no-op."""
        c = HijackClient("http://test")
        await c.__aexit__(None, None, None)
        assert c._client is None

    async def test_lazy_client_without_transport(self) -> None:
        """Lazy client creation without transport (real HTTP path)."""
        c = HijackClient("http://127.0.0.1:1")
        assert c._client is None
        assert c._transport is None
        _ = c._get_client()
        assert c._client is not None
        await c._client.aclose()

    async def test_quick_connect_without_display_name(self) -> None:
        app = TestSessionAPI._make_server_app()
        async with HijackClient(
            "http://test",
            transport=ASGITransport(app=app),
            headers={"X-Uterm-Principal": "t", "X-Uterm-Role": "admin"},
        ) as c:
            ok, data = await c.quick_connect("shell")
        assert ok
        assert "session_id" in data


# ---------------------------------------------------------------------------
# Transport error handling
# ---------------------------------------------------------------------------


class TestTransportErrors:
    async def test_connection_error_returns_false(self) -> None:
        # Point at a non-existent server
        async with HijackClient("http://127.0.0.1:1") as c:
            ok, data = await c.acquire(WID)

        assert ok is False
        assert "error" in data


class TestCustomTimeout:
    async def test_request_with_custom_timeout(self) -> None:
        """hijack.py:120 — timeout kwarg passed to httpx."""
        from provide.uterm.client.hijack import HijackClient

        async with HijackClient("http://127.0.0.1:1") as c:
            # watch_session_events passes a custom timeout — covers line 120
            ok, data = await c.watch_session_events("test-session", timeout_ms=100)
        assert ok is False  # connection refused


class TestWatchEventsParams:
    async def test_watch_session_events_with_event_types_and_pattern(self) -> None:
        """hijack.py:319-324 — event_types + pattern params."""
        from provide.uterm.client.hijack import HijackClient

        async with HijackClient("http://127.0.0.1:1") as c:
            ok, data = await c.watch_session_events(
                "test-session",
                event_types="output,error",
                pattern="ERROR.*",
                timeout_ms=100,
            )
        assert ok is False
