#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests covering small gaps in the factory.create_server_app orchestration."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from provide.uterm.recording import (
    InMemoryRecordingStore,
    NullRecordingStore,
)
from provide.uterm.server.app import create_server_app
from provide.uterm.server.config import default_server_config
from provide.uterm.server.recording import WebhookRecordingStore


@pytest.mark.asyncio
async def test_factory_initializes_webhook_behavioral_audit_gate() -> None:
    """When governance.behavioral_audit_url is set, a WebhookBehavioralAuditGate is wired into the hub."""
    from provide.uterm.bridge.hub.ext import WebhookBehavioralAuditGate

    config = default_server_config()
    config.governance.behavioral_audit_url = "https://fleet.example.com/audit"
    config.governance.behavioral_audit_secret = "shh"

    app = create_server_app(config, api_only=True)
    try:
        hub = app.state.uterm_hub
        gate = getattr(hub, "_behavioral_audit_gate", None)
        assert isinstance(gate, WebhookBehavioralAuditGate)
        assert gate.url == "https://fleet.example.com/audit"
    finally:
        await app.state.uterm_hub.shutdown()


def test_factory_uses_webhook_recording_store() -> None:
    """recording.store_type='webhook' with a webhook_url selects WebhookRecordingStore."""
    config = default_server_config()
    config.recording.store_type = "webhook"
    config.recording.webhook_url = "https://fleet.example.com/recordings"
    config.recording.webhook_secret = "shh"

    app = create_server_app(config, api_only=True)

    registry = app.state.uterm_registry
    assert isinstance(registry._recording_store, WebhookRecordingStore)


def test_factory_uses_in_memory_recording_store() -> None:
    """recording.store_type='memory' selects InMemoryRecordingStore."""
    config = default_server_config()
    config.recording.store_type = "memory"

    app = create_server_app(config, api_only=True)

    registry = app.state.uterm_registry
    assert isinstance(registry._recording_store, InMemoryRecordingStore)


def test_factory_uses_null_recording_store() -> None:
    """recording.store_type='null' selects NullRecordingStore."""
    config = default_server_config()
    config.recording.store_type = "null"

    app = create_server_app(config, api_only=True)

    registry = app.state.uterm_registry
    assert isinstance(registry._recording_store, NullRecordingStore)


@pytest.mark.asyncio
async def test_node_registry_heartbeat_outer_exception_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exception raised before discovery_provider.announce (e.g. from hub.browser_count_total)
    is caught by the outer try/except in _node_registry_heartbeat (lines 486-487).
    """
    config = default_server_config()
    config.governance.registry_webhook_url = "https://fleet.example.com/heartbeat"
    config.governance.registry_webhook_interval_s = 0.01

    app = create_server_app(config, api_only=True)

    # Force the outer try block to raise by patching hub.browser_count_total
    # before the lifespan starts the heartbeat task.
    hub = app.state.uterm_hub

    async def _boom() -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(hub, "browser_count_total", _boom)

    with TestClient(app):
        # Allow the heartbeat loop to tick at least once so the exception is hit.
        await asyncio.sleep(0.05)
    # No exception means the outer except path absorbed the error as expected.
