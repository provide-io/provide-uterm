"""Tests for DO alarm-based hijack lease auto-expiry (SessionRuntime.alarm)."""

from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import pytest
from provide.terminal.cloudflare.bridge.hijack import HijackSession
from provide.terminal.cloudflare.do.session_runtime import SessionRuntime


def _make_runtime() -> SessionRuntime:
    """Return a SessionRuntime backed by an in-memory SQLite DB."""
    conn = sqlite3.connect(":memory:")
    alarm_calls: list[int] = []
    ctx = SimpleNamespace(
        storage=SimpleNamespace(
            sql=SimpleNamespace(exec=conn.execute),
            setAlarm=alarm_calls.append,
        ),
        id=SimpleNamespace(name=lambda: "test-worker"),
    )
    runtime = SessionRuntime(ctx, {"AUTH_MODE": "dev"})
    runtime._alarm_calls = alarm_calls  # expose for assertions
    return runtime


@pytest.mark.asyncio
async def test_alarm_noop_when_no_session() -> None:
    runtime = _make_runtime()
    assert runtime.hijack.session is None
    await runtime.alarm()  # must not raise
    assert runtime.hijack.session is None


@pytest.mark.asyncio
async def test_alarm_releases_expired_lease() -> None:
    runtime = _make_runtime()
    runtime.hijack._session = HijackSession(
        hijack_id="hid-expired",
        owner="tester",
        lease_expires_at=time.monotonic() - 1,  # already expired
    )
    await runtime.alarm()
    assert runtime.hijack.session is None, "expired lease must be auto-released"


@pytest.mark.asyncio
async def test_alarm_reschedules_when_lease_still_valid() -> None:
    runtime = _make_runtime()
    future_expiry = time.monotonic() + 120
    runtime.hijack._session = HijackSession(
        hijack_id="hid-valid",
        owner="tester",
        lease_expires_at=future_expiry,
    )
    await runtime.alarm()
    # Lease should be kept.
    assert runtime.hijack.session is not None, "valid lease must not be released"
    # A new alarm should have been scheduled (wall-clock conversion for CF API).
    assert runtime._alarm_calls, "setAlarm must be called to reschedule"
    wall_expiry = future_expiry + (time.time() - time.monotonic())
    assert abs(runtime._alarm_calls[-1] - int(wall_expiry * 1000)) < 2000


@pytest.mark.asyncio
async def test_persist_lease_schedules_alarm() -> None:
    runtime = _make_runtime()
    result = runtime.hijack.acquire("owner", 60)
    assert result.ok and result.session is not None
    runtime.persist_lease(result.session)
    assert runtime._alarm_calls, "persist_lease must schedule a DO alarm"
    # Alarm uses wall-clock conversion of monotonic lease_expires_at
    wall_expires = result.session.lease_expires_at + (time.time() - time.monotonic())
    expected_ms = int(wall_expires * 1000)
    assert abs(runtime._alarm_calls[-1] - expected_ms) <= 2000
