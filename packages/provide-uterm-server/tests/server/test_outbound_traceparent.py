#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Fix 3c — outbound httpx calls propagate the W3C ``traceparent`` header.

Webhooks, governance gates, and the delegated IDP all build an outbound
``headers`` dict before POSTing. When a span is active, ``inject_trace_context``
stamps ``traceparent`` onto that dict so distributed traces survive the hop.

Crucially this goes through ``provide.telemetry`` (which is OpenTelemetry-
OPTIONAL) and imports NO ``opentelemetry`` — so the server never hard-requires
opentelemetry just to propagate trace context. These tests drive the trace
context via ``provide.telemetry.set_trace_context`` (the same contextvars a real
span sets) and never import opentelemetry.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from provide.telemetry import set_trace_context
from provide.uterm.server.auth import WebhookIdentityProvider
from provide.uterm.server.bridge.hub.ext import _build_webhook_headers
from provide.uterm.server.tracing import inject_trace_context
from provide.uterm.server.webhooks import WebhookManager
from tests.helpers import http_mock as respx

# Known fixed trace/span ids (W3C hex) so the emitted traceparent is deterministic.
_TRACE_ID = "1234567890abcdef1234567890abcdef"
_SPAN_ID = "1234567890abcdef"
_EXPECTED_TRACEPARENT = "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01"


@contextlib.contextmanager
def _active_span() -> Iterator[None]:
    """Set a provide.telemetry trace context (as an active span would), then clear it."""
    set_trace_context(_TRACE_ID, _SPAN_ID)
    try:
        yield
    finally:
        set_trace_context(None, None)


# ---------------------------------------------------------------------------
# 0) inject_trace_context branch matrix (no opentelemetry involved)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("trace_id", "span_id", "expect_header"),
    [
        (_TRACE_ID, _SPAN_ID, True),  # real ids → emit
        (None, None, False),  # no context → no emit
        ("0" * 32, _SPAN_ID, False),  # all-zero trace id (W3C invalid sentinel) → no emit
        (_TRACE_ID, "0" * 16, False),  # all-zero span id → no emit
        (_TRACE_ID, None, False),  # missing span id → no emit
    ],
)
def test_inject_trace_context_branches(trace_id: str | None, span_id: str | None, expect_header: bool) -> None:
    set_trace_context(trace_id, span_id)
    try:
        headers: dict[str, str] = {}
        inject_trace_context(headers)
    finally:
        set_trace_context(None, None)
    if expect_header:
        assert headers["traceparent"] == _EXPECTED_TRACEPARENT
    else:
        assert "traceparent" not in headers


# ---------------------------------------------------------------------------
# 1) shared header builder (covers all 4 governance gate classes)
# ---------------------------------------------------------------------------


def test_build_webhook_headers_injects_traceparent_inside_span() -> None:
    with _active_span():
        headers = _build_webhook_headers("uterm-test-secret-32-byte-minimum-key", b"body")  # pragma: allowlist secret

    assert headers["traceparent"] == _EXPECTED_TRACEPARENT
    # Existing header behaviour must remain intact.
    assert headers["Content-Type"] == "application/json"
    assert "X-Uterm-Signature" in headers
    assert "X-Uterm-Timestamp" in headers


def test_build_webhook_headers_no_traceparent_without_active_span() -> None:
    set_trace_context(None, None)
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
    """Exercise the real ``_deliver`` POST path under an active trace context."""
    manager = _make_manager()
    secret = "uterm-test-secret-32-byte-minimum-key"  # pragma: allowlist secret
    cfg = await manager.register("s1", "https://example.com/hook", secret=secret)

    captured_headers: list[dict[str, str]] = []

    async def _mock_post(*args: Any, **kwargs: Any) -> MagicMock:
        captured_headers.append(dict(kwargs.get("headers", {})))
        resp = MagicMock()
        resp.is_success = True
        return resp

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_mock_post)), _active_span():
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
    idp = WebhookIdentityProvider(
        url=url,
        secret="uterm-test-secret-32-byte-minimum-key",  # pragma: allowlist secret
        require_signed_response=False,  # this test asserts outbound traceparent, not response signing
    )

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
