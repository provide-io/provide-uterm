#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Fix 3c — outbound httpx calls propagate the W3C ``traceparent`` header.

Webhooks, governance gates, and the delegated IDP all build an outbound
``headers`` dict before POSTing. When the call runs inside an active span,
``opentelemetry.propagate.inject`` must stamp ``traceparent`` (and
``tracestate``) onto that dict so distributed traces survive the hop.

The OpenTelemetry **SDK** is not installed in the test environment, only
``opentelemetry-api``. The default (no-op) tracer provider produces no
``traceparent``. We therefore activate a ``NonRecordingSpan`` built from an
explicit *sampled* ``SpanContext`` — that is enough for the W3C propagator to
emit a ``traceparent`` using only the API package.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from provide.uterm.server.auth import WebhookIdentityProvider
from provide.uterm.server.bridge.hub.ext import _build_webhook_headers
from provide.uterm.server.webhooks import WebhookManager

# Known fixed trace/span ids so the emitted traceparent is deterministic.
_TRACE_ID = 0x1234567890ABCDEF1234567890ABCDEF
_SPAN_ID = 0x1234567890ABCDEF
_EXPECTED_TRACEPARENT = "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01"


@contextlib.contextmanager
def _active_span() -> Iterator[None]:
    """Activate a sampled span so the W3C propagator emits a traceparent.

    Uses a ``NonRecordingSpan`` so no SDK is required: the API-only propagator
    reads the span's ``SpanContext`` to format the ``traceparent`` value.
    """
    ctx = SpanContext(
        trace_id=_TRACE_ID,
        span_id=_SPAN_ID,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    with trace.use_span(NonRecordingSpan(ctx), end_on_exit=False):
        yield


# ---------------------------------------------------------------------------
# 1) shared header builder (covers all 4 governance gate classes)
# ---------------------------------------------------------------------------


def test_build_webhook_headers_injects_traceparent_inside_span() -> None:
    with _active_span():
        headers = _build_webhook_headers("uterm-test-secret-32-byte-minimum-key", b"body")

    assert headers["traceparent"] == _EXPECTED_TRACEPARENT
    # Existing header behaviour must remain intact.
    assert headers["Content-Type"] == "application/json"
    assert "X-Uterm-Signature" in headers
    assert "X-Uterm-Timestamp" in headers


def test_build_webhook_headers_no_traceparent_without_active_span() -> None:
    # No active recording span context → propagator is a no-op.
    headers = _build_webhook_headers(None, b"body")

    assert "traceparent" not in headers
    assert headers["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# 2) webhook delivery path
# ---------------------------------------------------------------------------


def _make_manager() -> WebhookManager:
    return WebhookManager(resolver=lambda _hostname: ("93.184.216.34",))


@pytest.mark.asyncio
async def test_webhook_delivery_includes_traceparent_end_to_end() -> None:
    """Exercise the real ``_deliver`` POST path under an active span.

    Delivery normally runs in a long-lived background loop decoupled from the
    span active when the event was enqueued, so the propagated trace context is
    the one active *at delivery time*. We therefore drive ``_deliver`` directly
    inside an active span — that is the genuine code path that builds the
    outbound ``headers`` dict and POSTs it.
    """
    manager = _make_manager()
    secret = "uterm-test-secret-32-byte-minimum-key"  # pragma: allowlist secret
    cfg = await manager.register("s1", "https://example.com/hook", secret=secret)

    captured_headers: list[dict[str, str]] = []

    async def _mock_post(*args: Any, **kwargs: Any) -> MagicMock:
        captured_headers.append(dict(kwargs.get("headers", {})))
        resp = MagicMock()
        resp.is_success = True
        return resp

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_mock_post)):
        with _active_span():
            await manager._deliver(cfg, {"type": "snapshot", "data": {"screen": "$ traced"}})

    assert captured_headers
    assert captured_headers[0]["traceparent"] == _EXPECTED_TRACEPARENT
    # HMAC signing still works alongside the injected trace headers.
    assert captured_headers[0]["X-Uterm-Signature"].startswith("sha256=")
    assert captured_headers[0]["Content-Type"] == "application/json"
    await manager.shutdown()


# ---------------------------------------------------------------------------
# 3) delegated IDP path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_webhook_idp_includes_traceparent() -> None:
    url = "https://auth.example.com/resolve"
    idp = WebhookIdentityProvider(url=url, secret="uterm-test-secret-32-byte-minimum-key")  # pragma: allowlist secret

    route = respx.post(url).mock(return_value=httpx.Response(200, json={"subject_id": "user-123", "roles": ["viewer"]}))

    class MockConnection:
        headers = {"Authorization": "Bearer some-token"}
        cookies: dict[str, str] = {}

    with _active_span():
        principal = await idp.resolve_principal(MockConnection())

    assert principal is not None
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["traceparent"] == _EXPECTED_TRACEPARENT
    # Content-Type and HMAC signature headers still present.
    assert sent.headers["content-type"] == "application/json"
    assert sent.headers["x-uterm-signature"].startswith("sha256=")
