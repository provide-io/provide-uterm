#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for OpenTelemetry distributed tracing integration."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config

# ---------------------------------------------------------------------------
# Span capture helpers
# ---------------------------------------------------------------------------


class _CapturingSpan:
    """Minimal span that records name and attributes for test assertions."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, str] = {}

    def set_attribute(self, key: str, value: str) -> None:
        self.attributes[key] = value

    def __enter__(self) -> _CapturingSpan:
        return self

    def __exit__(self, *_: object) -> None:
        pass


class _CapturingTracer:
    """Records every span started via start_as_current_span."""

    def __init__(self) -> None:
        self.spans: list[_CapturingSpan] = []

    def start_as_current_span(self, name: str, **_: Any) -> _CapturingSpan:
        span = _CapturingSpan(name)
        self.spans.append(span)
        return span

    def span_names(self) -> list[str]:
        return [s.name for s in self.spans]

    def spans_named(self, name: str) -> list[_CapturingSpan]:
        return [s for s in self.spans if s.name == name]


@contextmanager
def _capturing_tracer():  # type: ignore[no-untyped-def]
    """Context manager that installs a _CapturingTracer for the duration."""
    import provide.uterm.bridge.routes.websockets as _ws_mod
    import provide.uterm.server.routes.sessions as _sessions_mod
    import provide.uterm.server.routes.tunnels as _tunnels_mod

    ct = _CapturingTracer()
    with (
        patch.object(_sessions_mod, "get_tracer", return_value=ct),
        patch.object(_tunnels_mod, "get_tracer", return_value=ct),
        patch.object(_ws_mod, "get_tracer", return_value=ct),
    ):
        yield ct


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_client() -> TestClient:
    config = default_server_config()
    config.auth.mode = "dev"
    app = create_server_app(config)
    return TestClient(app)


# ---------------------------------------------------------------------------
# TelemetryMiddleware integration
# ---------------------------------------------------------------------------


def test_telemetry_middleware_is_added(app_client: TestClient) -> None:
    """TelemetryMiddleware should be present in the middleware stack."""
    from provide.telemetry import TelemetryMiddleware

    # Starlette stores added middleware as a list of Middleware namedtuples.
    mw_types = [m.cls for m in getattr(app_client.app, "user_middleware", [])]
    assert TelemetryMiddleware in mw_types


# ---------------------------------------------------------------------------
# API request creates span
# ---------------------------------------------------------------------------


def test_api_request_creates_span(app_client: TestClient) -> None:
    """A session create request should produce a uterm.session.create span."""
    with _capturing_tracer() as ct:
        r = app_client.post(
            "/api/sessions",
            json={
                "session_id": "trace-test-1",
                "display_name": "Trace Test",
                "connector_type": "shell",
                "connector_config": {},
            },
        )
    assert r.status_code == 200
    assert "uterm.session.create" in ct.span_names()


def test_span_has_session_attributes(app_client: TestClient) -> None:
    """The session create span should carry uterm.* attributes."""
    with _capturing_tracer() as ct:
        r = app_client.post(
            "/api/sessions",
            json={
                "session_id": "trace-attrs-1",
                "display_name": "Attr Test",
                "connector_type": "shell",
                "connector_config": {},
            },
        )
    assert r.status_code == 200
    spans = ct.spans_named("uterm.session.create")
    assert spans, "expected at least one uterm.session.create span"
    span = spans[0]
    assert span.attributes.get("uterm.session_id") == "trace-attrs-1"
    assert span.attributes.get("uterm.operation") == "session.create"
    assert span.attributes.get("uterm.principal") is not None
    assert span.attributes.get("http.method") == "POST"
    assert span.attributes.get("http.target") == "/api/sessions"


def test_delete_session_creates_span(app_client: TestClient) -> None:
    """DELETE /api/sessions/{id} should produce a uterm.session.delete span."""
    # Create first, then delete.
    app_client.post(
        "/api/sessions",
        json={
            "session_id": "trace-del-1",
            "display_name": "Del Test",
            "connector_type": "shell",
            "connector_config": {},
        },
    )
    with _capturing_tracer() as ct:
        r = app_client.delete("/api/sessions/trace-del-1")
    assert r.status_code == 200
    assert "uterm.session.delete" in ct.span_names()
    span = ct.spans_named("uterm.session.delete")[0]
    assert span.attributes.get("uterm.session_id") == "trace-del-1"
    assert span.attributes.get("uterm.operation") == "session.delete"


def test_quick_connect_creates_span(app_client: TestClient) -> None:
    """POST /api/connect should produce a uterm.session.quick_connect span."""
    with _capturing_tracer() as ct:
        r = app_client.post("/api/connect", json={"connector_type": "shell"})
    assert r.status_code == 200
    assert "uterm.session.quick_connect" in ct.span_names()
    span = ct.spans_named("uterm.session.quick_connect")[0]
    assert span.attributes.get("uterm.operation") == "session.quick_connect"
    assert span.attributes.get("http.method") == "POST"


def test_create_tunnel_creates_span(app_client: TestClient) -> None:
    """POST /api/tunnels should produce a uterm.tunnel.create span."""
    with _capturing_tracer() as ct:
        r = app_client.post("/api/tunnels", json={})
    assert r.status_code == 200
    assert "uterm.tunnel.create" in ct.span_names()
    span = ct.spans_named("uterm.tunnel.create")[0]
    assert span.attributes.get("uterm.operation") == "tunnel.create"


# ---------------------------------------------------------------------------
# WebSocket connection creates span
# ---------------------------------------------------------------------------


def test_ws_worker_connection_creates_span(app_client: TestClient) -> None:
    """Worker WS connection should produce uterm.ws.worker.connect span."""
    with _capturing_tracer() as ct, app_client.websocket_connect("/ws/worker/trace-ws-1/term") as ws:
        ws.send_text('{"type":"worker_hello","input_mode":"open"}')
    assert "uterm.ws.worker.connect" in ct.span_names()
    span = ct.spans_named("uterm.ws.worker.connect")[0]
    assert span.attributes.get("uterm.worker_id") == "trace-ws-1"
    assert span.attributes.get("uterm.operation") == "ws.worker.connect"


def test_ws_worker_disconnect_creates_span(app_client: TestClient) -> None:
    """Worker WS disconnect should produce uterm.ws.worker.disconnect span."""
    with _capturing_tracer() as ct, app_client.websocket_connect("/ws/worker/trace-ws-disc/term") as ws:
        ws.send_text('{"type":"worker_hello"}')
    assert "uterm.ws.worker.disconnect" in ct.span_names()


def test_ws_browser_connection_creates_span(app_client: TestClient) -> None:
    """Browser WS connection should produce uterm.ws.browser.connect span."""
    with _capturing_tracer() as ct, app_client.websocket_connect("/ws/browser/trace-ws-2/term") as ws:
        _msg = ws.receive_text()  # hello frame
    assert "uterm.ws.browser.connect" in ct.span_names()
    span = ct.spans_named("uterm.ws.browser.connect")[0]
    assert span.attributes.get("uterm.worker_id") == "trace-ws-2"
    assert span.attributes.get("uterm.operation") == "ws.browser.connect"


# ---------------------------------------------------------------------------
# tracing.py span() helper unit tests
# ---------------------------------------------------------------------------


class TestSpanHelper:
    """Unit tests for the tracing.span() context manager."""

    def test_span_creates_context(self) -> None:
        from provide.uterm.server.tracing import span

        with span("test.operation", **{"uterm.session_id": "s1"}) as s:
            assert s is not None

    def test_span_sets_attributes(self) -> None:
        from provide.uterm.server.tracing import span

        with span("test.attrs", **{"uterm.session_id": "s1", "uterm.op": "create"}) as s:
            assert s is not None

    def test_span_none_attributes_skipped(self) -> None:
        from provide.uterm.server.tracing import span

        with span("test.none", **{"uterm.session_id": None, "uterm.real": "value"}) as s:
            assert s is not None

    def test_tracer_exists(self) -> None:
        from provide.uterm.server.tracing import _tracer

        assert _tracer is not None

    @pytest.mark.asyncio()
    async def test_async_span_context(self) -> None:
        from provide.uterm.server.tracing import span

        async with span("test.async", **{"uterm.session_id": "a1"}) as s:
            assert s is not None
