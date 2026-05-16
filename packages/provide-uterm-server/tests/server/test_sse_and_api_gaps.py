#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Targeted tests to cover the remaining gaps in server routes.

Covers:
- sse.py line 69: GET /api/sessions/{id}/events/stream → 404 unknown session
- sse.py line 71: GET /api/sessions/{id}/events/stream → 403 insufficient privileges
- registry.py line 376: stream_session_events returns immediately when no EventBus
- registry.py lines 383-384: stream_session_events yields heartbeat on timeout
- api.py line 377: POST /api/sessions/{id}/annotate → 400 missing label
- api.py line 380: POST /api/sessions/{id}/annotate → 400 invalid severity
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.models import SessionDefinition

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(sessions: list[dict[str, Any]] | None = None) -> object:
    """Create a server app in dev mode with optional session definitions."""
    cfg = default_server_config()
    cfg.auth.mode = "dev"
    if sessions is not None:
        cfg.sessions = [SessionDefinition(**s) for s in sessions]  # type: ignore[arg-type]
    return create_server_app(cfg, api_only=True)


# ---------------------------------------------------------------------------
# sse.py line 69: stream_events → 404 when session not found
# ---------------------------------------------------------------------------


class TestSseStreamEvents404:
    """Covers sse.py line 69: HTTPException(404) when session is unknown."""

    def test_stream_events_unknown_session_returns_404(self) -> None:
        """GET /api/sessions/no-such/events/stream returns 404 for unknown session."""
        app = _make_app(sessions=[])
        with TestClient(app) as client:
            resp = client.get("/api/sessions/no-such-session/events/stream")
        assert resp.status_code == 404
        assert "unknown session" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# sse.py line 71: stream_events → 403 when caller cannot read session
# ---------------------------------------------------------------------------


class TestSseStreamEvents403:
    """Covers sse.py line 71: HTTPException(403) when caller lacks read access."""

    def test_stream_events_private_session_viewer_returns_403(self) -> None:
        """A viewer trying to stream a private session owned by someone else gets 403."""
        import time

        import jwt as _jwt

        _TEST_KEY = "uterm-test-secret-32-byte-minimum-key"

        from provide.uterm.server.models import AuthConfig

        cfg = default_server_config()
        now2 = int(time.time())
        worker_token = _jwt.encode(
            {
                "sub": "worker",
                "roles": ["admin"],
                "iss": "provide-uterm",
                "aud": "provide-uterm-server",
                "iat": now2,
                "nbf": now2,
                "exp": now2 + 600,
            },
            key=_TEST_KEY,
            algorithm="HS256",
        )
        cfg.auth = AuthConfig(
            mode="jwt",
            jwt_public_key_pem=_TEST_KEY,
            jwt_algorithms=["HS256"],
            jwt_issuer="provide-uterm",
            jwt_audience="provide-uterm-server",
            worker_bearer_token=worker_token,
        )
        cfg.sessions = [
            SessionDefinition(
                session_id="priv-s1",
                display_name="Private",
                connector_type="shell",
                visibility="private",  # type: ignore[arg-type]
                owner="other-user",
                auto_start=False,
            )
        ]
        app = create_server_app(cfg, api_only=True)

        now = int(time.time())
        viewer_token = _jwt.encode(
            {
                "sub": "viewer-user",
                "roles": ["viewer"],
                "iss": "provide-uterm",
                "aud": "provide-uterm-server",
                "iat": now,
                "nbf": now,
                "exp": now + 600,
            },
            key=_TEST_KEY,
            algorithm="HS256",
        )

        with TestClient(app) as client:
            resp = client.get(
                "/api/sessions/priv-s1/events/stream",
                headers={"Authorization": f"Bearer {viewer_token}"},
            )
        assert resp.status_code == 403
        assert "insufficient" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# sse.py lines 73-80: stream_events success path — returns StreamingResponse
# ---------------------------------------------------------------------------


class TestSseStreamEventsSuccess:
    """Covers sse.py lines 73-80: successful streaming response path."""

    async def test_stream_events_authorized_session_returns_streaming_response(self) -> None:
        """An authorized caller gets a 200 streaming response for a known session.

        Uses a hub without an EventBus so stream_session_events returns immediately,
        allowing the test to verify the response headers without blocking indefinitely.
        """
        from fastapi import FastAPI

        from provide.uterm.bridge.hub import TermHub
        from provide.uterm.server.routes.sse import create_sse_router

        # Build a minimal app with only the SSE router and a known session.
        hub = TermHub()  # NO EventBus — stream returns immediately
        app = FastAPI()

        # Attach the state the SSE route needs.
        from provide.uterm.server.models import SessionDefinition as _SD

        session_def = _SD(
            session_id="s-stream",
            display_name="Stream Test",
            connector_type="shell",
            auto_start=False,
        )

        # Create a mock registry that knows about the session.
        from unittest.mock import AsyncMock, MagicMock

        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=session_def)
        registry.stream_session_events = MagicMock(return_value=_empty_async_gen())
        registry._hub = hub

        authz = MagicMock()
        authz.can_read_session = AsyncMock(return_value=True)

        app.state.uterm_hub = hub
        app.state.uterm_registry = registry
        app.state.uterm_authz = authz

        # Add a middleware that sets the principal.
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.requests import Request as StarletteRequest

        class _FakePrincipal:
            subject_id = "dev-user"

        class _SetPrincipal(BaseHTTPMiddleware):
            async def dispatch(self, request: StarletteRequest, call_next: object) -> object:
                request.state.uterm_principal = _FakePrincipal()
                return await call_next(request)  # type: ignore[operator]

        app.add_middleware(_SetPrincipal)
        app.include_router(create_sse_router(), prefix="/api")

        with TestClient(app) as client, client.stream("GET", "/api/sessions/s-stream/events/stream") as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            # Consume all bytes — stream returns empty immediately (no EventBus).
            body = b"".join(resp.iter_bytes())
        assert body == b""


async def _empty_async_gen():  # type: ignore[return]
    """Async generator that yields nothing."""
    return
    yield  # noqa: unreachable


# ---------------------------------------------------------------------------
# registry.py line 376: stream_session_events returns when no EventBus
# ---------------------------------------------------------------------------


class TestRegistryStreamNoEventBus:
    """Covers registry.py line 376: generator returns immediately with no EventBus."""

    async def test_stream_session_events_no_event_bus_returns_empty(self) -> None:
        """When the hub has no EventBus, stream_session_events yields nothing."""
        from provide.uterm.bridge.hub import TermHub
        from provide.uterm.server.registry import SessionRegistry

        # Hub without EventBus.
        hub = TermHub()
        assert hub.event_bus is None

        registry = SessionRegistry.__new__(SessionRegistry)
        registry._hub = hub  # type: ignore[attr-defined]

        chunks: list[str] = []
        async for chunk in registry.stream_session_events("w1"):
            chunks.append(chunk)

        assert chunks == []


# ---------------------------------------------------------------------------
# registry.py lines 383-384: stream_session_events yields heartbeat on timeout
# ---------------------------------------------------------------------------


class TestRegistryStreamHeartbeat:
    """Covers registry.py lines 383-384 and 385-388: heartbeat and disconnect sentinel."""

    async def test_stream_session_events_emits_heartbeat_then_stops(self) -> None:
        """When no event arrives within heartbeat_s, a heartbeat line is yielded.
        Then the worker disconnect sentinel stops the generator."""
        from unittest.mock import AsyncMock

        from provide.uterm.bridge.hub import EventBus, TermHub
        from provide.uterm.server.registry import SessionRegistry

        hub = TermHub(event_bus=EventBus())

        # Register a fake worker.
        ws = AsyncMock()
        ws.send_text = AsyncMock()
        await hub.register_worker("w1", ws)

        registry = SessionRegistry.__new__(SessionRegistry)
        registry._hub = hub  # type: ignore[attr-defined]

        chunks: list[str] = []

        async def _emit_disconnect_after_delay() -> None:
            # Wait long enough for at least one heartbeat (heartbeat_s=0.05).
            await asyncio.sleep(0.12)
            # Disconnect the worker → sentinel → generator stops.
            assert hub.event_bus is not None
            hub.event_bus.close_worker("w1")

        task = asyncio.create_task(_emit_disconnect_after_delay())

        # Consume ALL chunks until the generator stops naturally.
        async for chunk in registry.stream_session_events("w1", heartbeat_s=0.05):
            chunks.append(chunk)

        await task

        # At least one heartbeat and the worker_disconnected sentinel.
        heartbeats = [c for c in chunks if '"type":"heartbeat"' in c]
        disconnects = [c for c in chunks if '"type":"worker_disconnected"' in c]
        assert len(heartbeats) >= 1
        assert len(disconnects) >= 1

    async def test_stream_session_events_yields_regular_event(self) -> None:
        """When an event arrives, it is yielded as a data: line (line 388)."""
        from unittest.mock import AsyncMock

        from provide.uterm.bridge.hub import EventBus, TermHub
        from provide.uterm.server.registry import SessionRegistry

        hub = TermHub(event_bus=EventBus())

        ws = AsyncMock()
        ws.send_text = AsyncMock()
        await hub.register_worker("w-evt", ws)

        registry = SessionRegistry.__new__(SessionRegistry)
        registry._hub = hub  # type: ignore[attr-defined]

        async def _emit_event_then_disconnect() -> None:
            await asyncio.sleep(0.02)
            await hub.append_event("w-evt", "term", {"data": "hello"})
            await asyncio.sleep(0.05)
            assert hub.event_bus is not None
            hub.event_bus.close_worker("w-evt")

        task = asyncio.create_task(_emit_event_then_disconnect())

        chunks: list[str] = []
        async for chunk in registry.stream_session_events("w-evt", heartbeat_s=10.0):
            chunks.append(chunk)

        await task

        # Should have received a regular event line.
        event_lines = [c for c in chunks if '"type":"worker_disconnected"' not in c and '"type":"heartbeat"' not in c]
        assert len(event_lines) >= 1
        assert '"term"' in event_lines[0] or '"data"' in event_lines[0]


# ---------------------------------------------------------------------------
# api.py line 377: annotate_session → 400 when label is missing
# ---------------------------------------------------------------------------


class TestAnnotateMissingLabel:
    """Covers api.py line 377: HTTPException(400) when label field is empty."""

    def test_annotate_session_missing_label_returns_400(self) -> None:
        """POST /api/sessions/{id}/annotate with no label field returns 400."""
        cfg = default_server_config()
        cfg.auth.mode = "dev"
        cfg.sessions = [
            SessionDefinition(
                session_id="provide-shell",
                display_name="Shell",
                connector_type="shell",
                auto_start=False,
            )
        ]
        with TestClient(create_server_app(cfg, api_only=True)) as client:
            # Ensure the runtime exists by connecting first.
            client.post("/api/sessions/provide-shell/connect")
            resp = client.post(
                "/api/sessions/provide-shell/annotate",
                json={"description": "no label here", "severity": "info"},
            )
        assert resp.status_code == 400
        assert "label" in resp.json()["detail"].lower()

    def test_annotate_session_empty_label_returns_400(self) -> None:
        """POST /api/sessions/{id}/annotate with an empty-string label returns 400."""
        cfg = default_server_config()
        cfg.auth.mode = "dev"
        with TestClient(create_server_app(cfg, api_only=True)) as client:
            client.post("/api/sessions/provide-shell/connect")
            resp = client.post(
                "/api/sessions/provide-shell/annotate",
                json={"label": "   ", "description": "", "severity": "info"},
            )
        assert resp.status_code == 400
        assert "label" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# api.py line 380: annotate_session → 400 when severity is invalid
# ---------------------------------------------------------------------------


class TestAnnotateInvalidSeverity:
    """Covers api.py line 380: HTTPException(400) when severity is not a valid value."""

    def test_annotate_session_invalid_severity_returns_400(self) -> None:
        """POST /api/sessions/{id}/annotate with an invalid severity returns 400."""
        cfg = default_server_config()
        cfg.auth.mode = "dev"
        with TestClient(create_server_app(cfg, api_only=True)) as client:
            client.post("/api/sessions/provide-shell/connect")
            resp = client.post(
                "/api/sessions/provide-shell/annotate",
                json={"label": "deploy", "description": "", "severity": "urgent"},
            )
        assert resp.status_code == 400
        assert "severity" in resp.json()["detail"].lower()
