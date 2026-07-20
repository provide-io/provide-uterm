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
    from provide.uterm.server.bridge.hub.ext import WebhookBehavioralAuditGate

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


async def test_factory_threads_max_workers_to_hub() -> None:
    """Fix 2b: config.max_workers is passed through to the constructed hub."""
    config = default_server_config()
    config.max_workers = 7

    app = create_server_app(config, api_only=True)
    try:
        assert app.state.uterm_hub.max_workers == 7
    finally:
        await app.state.uterm_hub.shutdown()


@pytest.mark.asyncio
async def test_factory_uses_explicit_hub_class() -> None:
    """hub_class=... takes the factory else-branch (not the default DeckMux hub)."""
    from provide.uterm.server.bridge.hub import TermHub

    class _CustomHub(TermHub):
        """Minimal subclass so isinstance proves the override path ran."""

    config = default_server_config()
    app = create_server_app(config, hub_class=_CustomHub, api_only=True)
    try:
        assert isinstance(app.state.uterm_hub, _CustomHub)
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


def _patch_fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the factory module's asyncio.sleep with a fast yield so sweep loops
    that sleep on their (>=1s) interval tick quickly under test."""
    import provide.uterm.server.app.factory_impl as factory_impl

    real_sleep = asyncio.sleep

    async def _fast_sleep(_delay: float) -> None:
        await real_sleep(0.001)

    monkeypatch.setattr(factory_impl.asyncio, "sleep", _fast_sleep)


@pytest.mark.asyncio
async def test_sqlite_reaper_logs_when_rows_deleted(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """With a sqlite backend, the reaper sweep is scheduled and runs; a non-zero
    return drives the ``if deleted:`` True branch (the log line)."""
    from pathlib import Path

    db_path = Path(str(tmp_path)) / "cp.db"
    config = default_server_config()
    config.control_plane.backend = "sqlite"
    config.control_plane.database_url = str(db_path)
    config.control_plane.reap_interval_s = 1

    app = create_server_app(config, api_only=True)
    _patch_fast_sleep(monkeypatch)

    called = asyncio.Event()

    async def _spy_reap(self: object, *, now: float, retention_s: int) -> int:
        called.set()
        return 3  # non-zero -> exercises the ``if deleted:`` log branch

    monkeypatch.setattr(type(app.state.uterm_control_plane), "reap", _spy_reap)

    with TestClient(app):
        await asyncio.wait_for(called.wait(), timeout=5.0)
    assert called.is_set()


@pytest.mark.asyncio
async def test_sqlite_reaper_runs_when_nothing_deleted(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """A reap returning 0 takes the ``if deleted:`` False branch (no log line)."""
    from pathlib import Path

    db_path = Path(str(tmp_path)) / "cp.db"
    config = default_server_config()
    config.control_plane.backend = "sqlite"
    config.control_plane.database_url = str(db_path)
    config.control_plane.reap_interval_s = 1

    app = create_server_app(config, api_only=True)
    _patch_fast_sleep(monkeypatch)

    called = asyncio.Event()

    async def _spy_reap(self: object, *, now: float, retention_s: int) -> int:
        called.set()
        return 0  # nothing deleted -> ``if deleted:`` False branch

    monkeypatch.setattr(type(app.state.uterm_control_plane), "reap", _spy_reap)

    with TestClient(app):
        await asyncio.wait_for(called.wait(), timeout=5.0)
    assert called.is_set()


@pytest.mark.asyncio
async def test_sqlite_reaper_error_is_swallowed(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """A reap that raises is caught by the sweep's except Exception branch and
    does not crash the lifespan."""
    from pathlib import Path

    db_path = Path(str(tmp_path)) / "cp.db"
    config = default_server_config()
    config.control_plane.backend = "sqlite"
    config.control_plane.database_url = str(db_path)
    config.control_plane.reap_interval_s = 1

    app = create_server_app(config, api_only=True)
    _patch_fast_sleep(monkeypatch)

    called = asyncio.Event()

    async def _boom_reap(self: object, *, now: float, retention_s: int) -> int:
        called.set()
        raise RuntimeError("boom")

    monkeypatch.setattr(type(app.state.uterm_control_plane), "reap", _boom_reap)

    with TestClient(app):
        await asyncio.wait_for(called.wait(), timeout=5.0)
    # No exception escaping means the except path absorbed the error.
    assert called.is_set()


@pytest.mark.asyncio
async def test_memory_backend_schedules_reaper(monkeypatch: pytest.MonkeyPatch) -> None:
    """The memory backend now does real soft-delete reaping, so the reaper sweep
    is scheduled unconditionally and reap() is called."""
    config = default_server_config()  # defaults to memory backend
    assert config.control_plane.backend == "memory"
    config.control_plane.reap_interval_s = 1

    app = create_server_app(config, api_only=True)
    _patch_fast_sleep(monkeypatch)

    called = asyncio.Event()

    async def _spy_reap(self: object, *, now: float, retention_s: int) -> int:
        called.set()
        return 0

    monkeypatch.setattr(type(app.state.uterm_control_plane), "reap", _spy_reap)

    with TestClient(app):
        await asyncio.wait_for(called.wait(), timeout=5.0)
    assert called.is_set()


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


@pytest.mark.asyncio
async def test_approval_sweep_invokes_cleanup_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lifespan schedules _sweep_expired_approvals, which calls
    hub.approval_store.cleanup_expired() at least once in production."""
    config = default_server_config()

    app = create_server_app(config, api_only=True)
    _patch_fast_sleep(monkeypatch)

    called = asyncio.Event()
    real_cleanup = app.state.uterm_hub.approval_store.cleanup_expired

    async def _spy_cleanup() -> None:
        called.set()
        await real_cleanup()

    monkeypatch.setattr(app.state.uterm_hub.approval_store, "cleanup_expired", _spy_cleanup)

    with TestClient(app):
        await asyncio.wait_for(called.wait(), timeout=5.0)
    assert called.is_set()


@pytest.mark.asyncio
async def test_approval_sweep_times_out_expired_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """An expired PENDING approval added before startup is transitioned to TIMEOUT
    by the scheduled approval sweep."""
    import time

    from provide.uterm.server.bridge.hub.approvals import (
        ApprovalRequest,
        ApprovalStatus,
    )

    config = default_server_config()

    app = create_server_app(config, api_only=True)
    _patch_fast_sleep(monkeypatch)

    store = app.state.uterm_hub.approval_store
    now = time.time()
    req = ApprovalRequest(
        id="req-expired",
        worker_id="w1",
        submitter_id="s1",
        command="ls",
        status=ApprovalStatus.PENDING,
        created_at=now - 100,
        expires_at=now - 50,
    )
    store.add(req)

    async def _is_timeout() -> bool:
        while store.get("req-expired").status != ApprovalStatus.TIMEOUT:
            await asyncio.sleep(0.001)
        return True

    with TestClient(app):
        assert await asyncio.wait_for(_is_timeout(), timeout=5.0)
    assert store.get("req-expired").status == ApprovalStatus.TIMEOUT


@pytest.mark.asyncio
async def test_approval_sweep_error_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cleanup_expired that raises is caught by the sweep's except Exception
    branch and does not crash the lifespan."""
    config = default_server_config()

    app = create_server_app(config, api_only=True)
    _patch_fast_sleep(monkeypatch)

    called = asyncio.Event()

    async def _boom_cleanup() -> None:
        called.set()
        raise RuntimeError("boom")

    monkeypatch.setattr(app.state.uterm_hub.approval_store, "cleanup_expired", _boom_cleanup)

    with TestClient(app):
        await asyncio.wait_for(called.wait(), timeout=5.0)
    # No exception escaping means the except path absorbed the error.
    assert called.is_set()


@pytest.mark.asyncio
async def test_approval_sweep_cancellation_is_reraised(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the lifespan cancels the approval sweep while cleanup_expired is
    awaiting, the CancelledError must propagate (not be swallowed) so the task
    actually stops — this drives the ``except asyncio.CancelledError: raise``
    branch."""
    config = default_server_config()

    app = create_server_app(config, api_only=True)
    _patch_fast_sleep(monkeypatch)

    inside = asyncio.Event()

    async def _block_cleanup() -> None:
        # Park inside the sweep's try block so the lifespan's cancel lands on
        # an awaiting cleanup_expired, raising CancelledError from here.
        inside.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app.state.uterm_hub.approval_store, "cleanup_expired", _block_cleanup)

    with TestClient(app):
        # Ensure the sweep task is parked inside cleanup_expired before the
        # lifespan teardown cancels it.
        await asyncio.wait_for(inside.wait(), timeout=5.0)
    # Reaching here means the lifespan teardown completed cleanly: the
    # CancelledError was re-raised and awaited without escaping.
    assert inside.is_set()
