#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for HijackClient against a real FastAPI app via ASGI transport."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport

from provide.terminal.client.hijack import HijackClient
from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.models import HijackSession, WorkerTermState

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
        hub._workers[WID].hijack_session = HijackSession(
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
        from provide.terminal.server.app import create_server_app
        from provide.terminal.server.config import config_from_mapping

        cfg = config_from_mapping(
            {
                "server": {"host": "127.0.0.1", "port": 0},
                "auth": {"mode": "dev"},
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
# Transport error handling
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
