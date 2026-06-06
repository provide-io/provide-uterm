#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the TelemetrySink open-core hook (upward Node→Fleet-Manager telemetry).

Coverage targets:
- TelemetryEvent model construction and fields
- NoOpTelemetrySink (emit is a no-op)
- WebhookTelemetrySink: successful POST
- WebhookTelemetrySink: fail-open on transport error
- WebhookTelemetrySink: fail-open on egress check error
- TelemetrySink runtime-checkable Protocol check
- GovernanceConfig telemetry fields + validator
- factory wiring: no-sink (not configured) and webhook-sink branches
- hub emit_telemetry: no-sink branch (NoneType)
- hub emit_telemetry: with-sink happy path
- hub emit_telemetry: fail-open when sink raises
- lifecycle emission: hijack.acquired (REST)
- lifecycle emission: hijack.acquired (dashboard WS)
- lifecycle emission: hijack.released (dashboard WS)
- lifecycle emission: hijack.expired (REST and dashboard)
- lifecycle emission: session.registered (worker)
- lifecycle emission: session.registered (browser)
- lifecycle emission: session.disconnected (browser)
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from provide.uterm.server.bridge.hub.ext import (
    NoOpTelemetrySink,
    TelemetryEvent,
    TelemetrySink,
    WebhookTelemetrySink,
)

# ---------------------------------------------------------------------------
# TelemetryEvent model
# ---------------------------------------------------------------------------


def test_telemetry_event_required_fields() -> None:
    evt = TelemetryEvent(event_type="session.registered", worker_id="w1", timestamp=1.0)
    assert evt.event_type == "session.registered"
    assert evt.worker_id == "w1"
    assert evt.principal is None
    assert evt.role is None
    assert evt.metadata == {}
    assert evt.timestamp == 1.0


def test_telemetry_event_all_fields() -> None:
    evt = TelemetryEvent(
        event_type="hijack.acquired",
        worker_id="w2",
        principal="alice",
        role="operator",
        metadata={"hijack_type": "rest", "lease_s": 60},
        timestamp=42.0,
    )
    assert evt.principal == "alice"
    assert evt.role == "operator"
    assert evt.metadata["hijack_type"] == "rest"


def test_telemetry_event_metadata_default_factory_is_per_instance() -> None:
    """Each instance must get its own metadata dict (not shared default)."""
    a = TelemetryEvent(event_type="x", worker_id="w", timestamp=0.0)
    b = TelemetryEvent(event_type="y", worker_id="w", timestamp=0.0)
    a.metadata["k"] = "v"
    assert "k" not in b.metadata


# ---------------------------------------------------------------------------
# TelemetrySink Protocol
# ---------------------------------------------------------------------------


def test_telemetry_sink_protocol_is_runtime_checkable() -> None:
    noop = NoOpTelemetrySink()
    assert isinstance(noop, TelemetrySink)

    webhook = WebhookTelemetrySink(url="https://telemetry.example/ingest")
    assert isinstance(webhook, TelemetrySink)


# ---------------------------------------------------------------------------
# NoOpTelemetrySink
# ---------------------------------------------------------------------------


async def test_noop_telemetry_sink_emit_returns_none() -> None:
    sink = NoOpTelemetrySink()
    evt = TelemetryEvent(event_type="x", worker_id="w", timestamp=0.0)
    result = await sink.emit(evt)
    assert result is None


# ---------------------------------------------------------------------------
# WebhookTelemetrySink — success
# ---------------------------------------------------------------------------


async def test_webhook_telemetry_sink_posts_on_emit(respx_mock) -> None:
    route = respx_mock.post("https://telemetry.example/ingest").mock(return_value=httpx.Response(200))
    sink = WebhookTelemetrySink(url="https://telemetry.example/ingest")
    evt = TelemetryEvent(event_type="session.registered", worker_id="w1", timestamp=time.time())
    await sink.emit(evt)
    assert route.called


async def test_webhook_telemetry_sink_posts_with_secret(respx_mock) -> None:
    route = respx_mock.post("https://telemetry.example/ingest").mock(return_value=httpx.Response(200))
    sink = WebhookTelemetrySink(url="https://telemetry.example/ingest", secret="s3cr3t")
    evt = TelemetryEvent(event_type="hijack.acquired", worker_id="w1", timestamp=time.time())
    await sink.emit(evt)
    assert route.called
    req = route.calls[0].request
    assert req.headers.get("X-Uterm-Signature")


async def test_webhook_telemetry_sink_non_200_does_not_raise(respx_mock) -> None:
    respx_mock.post("https://telemetry.example/ingest").mock(return_value=httpx.Response(500))
    sink = WebhookTelemetrySink(url="https://telemetry.example/ingest")
    evt = TelemetryEvent(event_type="x", worker_id="w", timestamp=0.0)
    # Must not raise even on server error
    await sink.emit(evt)


# ---------------------------------------------------------------------------
# WebhookTelemetrySink — fail-open (transport error)
# ---------------------------------------------------------------------------


async def test_webhook_telemetry_sink_fail_open_on_transport_error() -> None:
    """A connection-refused error must be swallowed — sink is always fail-open."""
    sink = WebhookTelemetrySink(url="http://127.0.0.1:1/never", timeout_s=0.05)
    evt = TelemetryEvent(event_type="x", worker_id="w", timeout=0.0, timestamp=0.0)
    # Must not raise
    await sink.emit(evt)


async def test_webhook_telemetry_sink_fail_open_on_egress_exception() -> None:
    """Egress check raising must also be absorbed."""
    sink = WebhookTelemetrySink(url="https://telemetry.example/ingest")
    evt = TelemetryEvent(event_type="x", worker_id="w", timestamp=0.0)
    with patch(
        # ext imports the egress guard lazily (to break an import cycle), so the
        # mock must target the source module, not ext's namespace.
        "provide.uterm.server.egress.assert_webhook_target_allowed",
        AsyncMock(side_effect=RuntimeError("egress denied")),
    ):
        # Must not raise
        await sink.emit(evt)


# ---------------------------------------------------------------------------
# GovernanceConfig — telemetry fields and validator
# ---------------------------------------------------------------------------


def test_governance_config_telemetry_fields_default_none() -> None:
    from provide.uterm.server.config_schema import GovernanceConfig

    cfg = GovernanceConfig()
    assert cfg.telemetry_webhook_url is None
    assert cfg.telemetry_webhook_secret is None
    assert cfg.telemetry_webhook_timeout_s == 2.0


def test_governance_config_telemetry_accepts_https() -> None:
    from provide.uterm.server.config_schema import GovernanceConfig

    cfg = GovernanceConfig(
        telemetry_webhook_url="https://telemetry.example/ingest",
        telemetry_webhook_secret="tok",  # pragma: allowlist secret
        telemetry_webhook_timeout_s=5.0,
    )
    assert cfg.telemetry_webhook_url == "https://telemetry.example/ingest"
    assert cfg.telemetry_webhook_timeout_s == 5.0


def test_governance_config_telemetry_rejects_http_non_loopback() -> None:
    from provide.uterm.server.config_schema import GovernanceConfig

    with pytest.raises(ValueError, match="telemetry_webhook_url"):
        GovernanceConfig(telemetry_webhook_url="http://remote.example/ingest")


def test_governance_config_telemetry_allows_http_loopback() -> None:
    from provide.uterm.server.config_schema import GovernanceConfig

    cfg = GovernanceConfig(telemetry_webhook_url="http://127.0.0.1:9000/ingest")
    assert cfg.telemetry_webhook_url == "http://127.0.0.1:9000/ingest"


# ---------------------------------------------------------------------------
# Factory wiring
# ---------------------------------------------------------------------------


async def test_factory_wires_no_telemetry_sink_when_not_configured() -> None:
    from provide.uterm.server.app import create_server_app
    from provide.uterm.server.models import AuthConfig, GovernanceConfig, ServerConfig

    config = ServerConfig(
        auth=AuthConfig(mode="dev_token"),
        governance=GovernanceConfig(),
    )
    app = create_server_app(config, api_only=True)
    assert app.state.uterm_hub._telemetry_sink is None


async def test_factory_wires_webhook_telemetry_sink_when_configured() -> None:
    from provide.uterm.server.app import create_server_app
    from provide.uterm.server.models import AuthConfig, GovernanceConfig, ServerConfig

    config = ServerConfig(
        auth=AuthConfig(mode="dev_token"),
        governance=GovernanceConfig(
            telemetry_webhook_url="https://telemetry.example/ingest",
            telemetry_webhook_secret="mysecret",  # pragma: allowlist secret
            telemetry_webhook_timeout_s=3.0,
        ),
    )
    app = create_server_app(config, api_only=True)
    sink = app.state.uterm_hub._telemetry_sink
    assert isinstance(sink, WebhookTelemetrySink)
    assert sink.url == "https://telemetry.example/ingest"
    assert sink.secret == "mysecret"  # pragma: allowlist secret
    assert sink.timeout == 3.0


# ---------------------------------------------------------------------------
# Hub emit_telemetry
# ---------------------------------------------------------------------------


async def test_hub_emit_telemetry_no_sink_is_noop() -> None:
    from provide.uterm.server.bridge.hub import TermHub

    hub = TermHub()
    assert hub._telemetry_sink is None
    # Must return without error when no sink is configured
    await hub.emit_telemetry("session.registered", worker_id="w1")


async def test_hub_emit_telemetry_with_sink_calls_emit() -> None:
    from provide.uterm.server.bridge.hub import TermHub

    mock_sink = AsyncMock()
    hub = TermHub(telemetry_sink=mock_sink)
    await hub.emit_telemetry("hijack.acquired", worker_id="w1", principal="alice", role="operator")
    mock_sink.emit.assert_awaited_once()
    evt: TelemetryEvent = mock_sink.emit.call_args[0][0]
    assert evt.event_type == "hijack.acquired"
    assert evt.worker_id == "w1"
    assert evt.principal == "alice"
    assert evt.role == "operator"
    assert isinstance(evt.timestamp, float)


async def test_hub_emit_telemetry_fail_open_when_sink_raises() -> None:
    from provide.uterm.server.bridge.hub import TermHub

    failing_sink = AsyncMock()
    failing_sink.emit.side_effect = RuntimeError("unexpected sink failure")
    hub = TermHub(telemetry_sink=failing_sink)
    # Must not raise
    await hub.emit_telemetry("x", worker_id="w1")


async def test_hub_emit_telemetry_passes_metadata() -> None:
    from provide.uterm.server.bridge.hub import TermHub

    mock_sink = AsyncMock()
    hub = TermHub(telemetry_sink=mock_sink)
    await hub.emit_telemetry("hijack.expired", worker_id="w1", metadata={"hijack_type": "rest"})
    evt: TelemetryEvent = mock_sink.emit.call_args[0][0]
    assert evt.metadata == {"hijack_type": "rest"}


async def test_hub_emit_telemetry_empty_metadata_default() -> None:
    from provide.uterm.server.bridge.hub import TermHub

    mock_sink = AsyncMock()
    hub = TermHub(telemetry_sink=mock_sink)
    await hub.emit_telemetry("x", worker_id="w1")
    evt: TelemetryEvent = mock_sink.emit.call_args[0][0]
    assert evt.metadata == {}


# ---------------------------------------------------------------------------
# Lifecycle emission — hijack events via lease.py
# ---------------------------------------------------------------------------


async def test_telemetry_emitted_on_rest_hijack_acquired() -> None:
    from provide.uterm.server.bridge.hub import TermHub

    mock_sink = AsyncMock()
    hub = TermHub(telemetry_sink=mock_sink)
    worker_ws = AsyncMock()
    worker_id = "w1"

    await hub.register_worker(worker_id, worker_ws)
    mock_sink.emit.reset_mock()

    ok, _ = await hub.try_acquire_rest_hijack(
        worker_id, owner="alice", lease_s=60, hijack_id="hid-1", now=time.monotonic()
    )
    assert ok
    emitted = [c[0][0] for c in mock_sink.emit.call_args_list]
    acquired = [e for e in emitted if e.event_type == "hijack.acquired"]
    assert acquired, "hijack.acquired telemetry must be emitted on REST acquire"
    assert acquired[0].principal == "alice"
    assert acquired[0].metadata.get("hijack_type") == "rest"


async def test_telemetry_emitted_on_ws_hijack_acquired() -> None:
    from provide.uterm.server.bridge.hub import TermHub

    mock_sink = AsyncMock()
    hub = TermHub(telemetry_sink=mock_sink)
    worker_ws = AsyncMock()
    browser_ws = AsyncMock()
    worker_id = "w1"

    await hub.register_worker(worker_id, worker_ws)
    await hub.register_browser(worker_id, browser_ws, "admin")
    mock_sink.emit.reset_mock()

    ok, _ = await hub.try_acquire_ws_hijack(worker_id, browser_ws)
    assert ok
    emitted = [c[0][0] for c in mock_sink.emit.call_args_list]
    acquired = [e for e in emitted if e.event_type == "hijack.acquired"]
    assert acquired, "hijack.acquired telemetry must be emitted on WS acquire"
    assert acquired[0].metadata.get("hijack_type") == "dashboard"


async def test_telemetry_emitted_on_ws_hijack_released() -> None:
    from provide.uterm.server.bridge.hub import TermHub

    mock_sink = AsyncMock()
    hub = TermHub(telemetry_sink=mock_sink)
    worker_ws = AsyncMock()
    browser_ws = AsyncMock()
    worker_id = "w1"

    await hub.register_worker(worker_id, worker_ws)
    await hub.register_browser(worker_id, browser_ws, "admin")
    await hub.try_acquire_ws_hijack(worker_id, browser_ws)
    mock_sink.emit.reset_mock()

    ok, _ = await hub.try_release_ws_hijack(worker_id, browser_ws)
    assert ok
    emitted = [c[0][0] for c in mock_sink.emit.call_args_list]
    released = [e for e in emitted if e.event_type == "hijack.released"]
    assert released, "hijack.released telemetry must be emitted on WS release"
    assert released[0].metadata.get("hijack_type") == "dashboard"


async def test_telemetry_emitted_on_rest_hijack_expired() -> None:
    from provide.uterm.server.bridge.hub import TermHub

    mock_sink = AsyncMock()
    hub = TermHub(telemetry_sink=mock_sink)
    worker_ws = AsyncMock()
    worker_id = "w1"

    await hub.register_worker(worker_id, worker_ws)
    # Acquire with a lease that is already expired
    past = time.monotonic() - 1000
    ok, _ = await hub.try_acquire_rest_hijack(worker_id, owner="alice", lease_s=1, hijack_id="hid-x", now=past)
    assert ok
    mock_sink.emit.reset_mock()

    cleaned = await hub.cleanup_expired_hijack(worker_id)
    assert cleaned
    emitted = [c[0][0] for c in mock_sink.emit.call_args_list]
    expired = [e for e in emitted if e.event_type == "hijack.expired"]
    assert expired, "hijack.expired telemetry must be emitted on REST lease expiry"
    assert expired[0].metadata.get("hijack_type") == "rest"


# ---------------------------------------------------------------------------
# Lifecycle emission — session events via connection.py
# ---------------------------------------------------------------------------


async def test_telemetry_emitted_on_worker_registered() -> None:
    from provide.uterm.server.bridge.hub import TermHub

    mock_sink = AsyncMock()
    hub = TermHub(telemetry_sink=mock_sink)
    worker_ws = AsyncMock()
    mock_sink.emit.reset_mock()

    await hub.register_worker("w1", worker_ws)
    emitted = [c[0][0] for c in mock_sink.emit.call_args_list]
    registered = [e for e in emitted if e.event_type == "session.registered"]
    assert registered, "session.registered telemetry must be emitted on worker register"
    assert registered[0].metadata.get("session_type") == "worker"


async def test_telemetry_emitted_on_browser_registered() -> None:
    from provide.uterm.server.bridge.hub import TermHub

    mock_sink = AsyncMock()
    hub = TermHub(telemetry_sink=mock_sink)
    browser_ws = AsyncMock()
    mock_sink.emit.reset_mock()

    await hub.register_browser("w1", browser_ws, "operator")
    emitted = [c[0][0] for c in mock_sink.emit.call_args_list]
    registered = [e for e in emitted if e.event_type == "session.registered"]
    assert registered, "session.registered telemetry must be emitted on browser register"
    assert registered[0].metadata.get("session_type") == "browser"
    assert registered[0].role == "operator"


async def test_telemetry_emitted_on_browser_disconnected() -> None:
    from provide.uterm.server.bridge.hub import TermHub

    mock_sink = AsyncMock()
    hub = TermHub(telemetry_sink=mock_sink)
    browser_ws = AsyncMock()

    await hub.register_browser("w1", browser_ws, "viewer")
    mock_sink.emit.reset_mock()

    await hub.cleanup_browser_disconnect("w1", browser_ws, owned_hijack=False)
    emitted = [c[0][0] for c in mock_sink.emit.call_args_list]
    disconnected = [e for e in emitted if e.event_type == "session.disconnected"]
    assert disconnected, "session.disconnected telemetry must be emitted on browser disconnect"
    assert disconnected[0].metadata.get("session_type") == "browser"
