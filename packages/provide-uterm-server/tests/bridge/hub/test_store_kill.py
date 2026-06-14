#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Isolation + mutation-killing suite for :class:`StateStore` (hub/store.py).

``StateStore`` holds only a back reference to the composing ``TermHub`` and
reaches a small, well-defined set of hub attributes (``_lock``,
``_input_buffers``, ``max_buffer_chars``, ``registry``, ``_background_tasks``,
the callbacks, the identity plumbing). This suite drives it against a hand-written
fake hub that implements exactly that surface — so each behavioural branch is
pinned without standing up a full hub, and the predicates/clamps/line-buffer
logic that the incidental hub suites leave unbound are killed deterministically.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest

from provide.uterm.bridge.coordinator import HijackSession
from provide.uterm.server.bridge.hub import store as store_module
from provide.uterm.server.bridge.hub.store import StateStore
from provide.uterm.server.bridge.models import WorkerTermState


class _FakeRegistry:
    def __init__(self) -> None:
        self._workers: dict[str, Any] = {}

    def get(self, worker_id: str) -> Any:
        return self._workers.get(worker_id)


class _FakeHub:
    """Implements exactly the attribute surface StateStore dereferences."""

    def __init__(
        self,
        *,
        max_buffer_chars: int = 40_000,
        on_metric: Any = None,
        on_hijack_changed: Any = None,
        resolve_browser_role: Any = None,
        identity_provider: Any = None,
        delegate_roles: bool = True,
    ) -> None:
        self._lock = asyncio.Lock()
        self._input_buffers: dict[Any, str] = {}
        self.max_buffer_chars = max_buffer_chars
        self.registry = _FakeRegistry()
        self._background_tasks: set[Any] = set()
        self._on_metric = on_metric
        self._on_hijack_changed = on_hijack_changed
        self._resolve_browser_role = resolve_browser_role
        self._identity_provider = identity_provider
        self._delegate_roles = delegate_roles


def _store(**kw: Any) -> tuple[StateStore, _FakeHub]:
    hub = _FakeHub(**kw)
    return StateStore(hub), hub


# == buffer_and_get_command ==================================================


def test_buffer_partial_input_accumulates() -> None:
    store, hub = _store()
    ws = object()
    assert store.buffer_and_get_command(ws, "ls ") is None
    assert hub._input_buffers[ws] == "ls "  # stored (pins the buffer write + None return)


def test_buffer_concatenates_prior_segment() -> None:
    """The ``get(ws, "") + data`` accumulation pins both the default and the concat."""
    store, hub = _store()
    ws = object()
    store.buffer_and_get_command(ws, "ab")
    assert store.buffer_and_get_command(ws, "cd\n") == "abcd\n"
    assert ws not in hub._input_buffers  # popped on completion


def test_buffer_newline_returns_command_and_clears() -> None:
    store, hub = _store()
    ws = object()
    assert store.buffer_and_get_command(ws, "help\n") == "help\n"
    assert ws not in hub._input_buffers


def test_buffer_carriage_return_returns_command() -> None:
    """``"\\r" in buf or "\\n" in buf`` — a CR alone must also flush (pins the ``or``)."""
    store, _ = _store()
    assert store.buffer_and_get_command(object(), "help\r") == "help\r"


def test_buffer_at_limit_is_kept_over_limit_is_dropped() -> None:
    """``len(buf) > max`` is strict: exactly max is kept, max+1 is discarded."""
    store, hub = _store(max_buffer_chars=4)
    ws = object()
    # len == max (4) and no newline → kept, returns None.
    assert store.buffer_and_get_command(ws, "abcd") is None
    assert hub._input_buffers[ws] == "abcd"
    # Next char makes it 5 (> 4) → discarded, returns None, buffer cleared.
    assert store.buffer_and_get_command(ws, "e") is None
    assert ws not in hub._input_buffers


def test_buffer_over_limit_on_first_write_does_not_raise() -> None:
    """A single oversized write (ws never buffered) discards cleanly.

    Pins ``pop(ws, None)``: without the default, popping an absent ws raises
    KeyError on the first oversized write.
    """
    store, hub = _store(max_buffer_chars=4)
    ws = object()
    assert store.buffer_and_get_command(ws, "abcdef") is None  # 6 > 4, ws not yet buffered
    assert ws not in hub._input_buffers


# == shutdown ================================================================


async def test_shutdown_no_tasks_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    store, _ = _store()
    with caplog.at_level(logging.INFO, logger="provide.uterm.server.bridge.hub"):
        await store.shutdown()
    assert not any("hub_shutdown" in r.message for r in caplog.records)


async def test_shutdown_cancels_tasks_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    store, hub = _store()

    async def _never() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(_never())
    hub._background_tasks.add(task)
    with caplog.at_level(logging.INFO, logger="provide.uterm.server.bridge.hub"):
        await store.shutdown()
    assert task.cancelled() or task.done()
    # Pins `if count:`, the count arg (else %d → TypeError) + the text. Anchoring on
    # the structlog logger suffix breaks an "XX…XX"-wrapped event-string mutant.
    assert any(
        "hub_shutdown cancelled 1 background tasks [provide.uterm.server.bridge.hub.store]" in r.getMessage()
        for r in caplog.records
    )


# == touch_activity / get_or_create =========================================


async def test_touch_activity_updates_present_worker() -> None:
    store, hub = _store()
    st = WorkerTermState()
    st.last_activity_at = 1.0
    hub.registry._workers["w"] = st
    await store.touch_activity("w")
    assert st.last_activity_at > 1.0  # monotonic stamp written (pins the assignment)


async def test_touch_activity_missing_worker_is_noop() -> None:
    store, _ = _store()
    await store.touch_activity("ghost")  # must not raise


async def test_get_or_create_creates_then_returns_same() -> None:
    store, hub = _store()
    created = await store.get_or_create("w")
    assert isinstance(created, WorkerTermState)
    assert hub.registry._workers["w"] is created
    again = await store.get_or_create("w")
    assert again is created  # existing returned, not replaced


# == metric ==================================================================


def test_metric_no_callback_is_noop() -> None:
    store, _ = _store(on_metric=None)
    store.metric("x")  # must not raise


def test_metric_invokes_callback_with_int_value() -> None:
    seen: list[tuple[str, int]] = []
    store, _ = _store(on_metric=lambda name, value: seen.append((name, value)))
    store.metric("hits", 2.9)
    assert seen == [("hits", 2)]  # int() coercion + name passthrough


def test_metric_default_value_is_one() -> None:
    seen: list[tuple[str, int]] = []
    store, _ = _store(on_metric=lambda name, value: seen.append((name, value)))
    store.metric("hits")
    assert seen == [("hits", 1)]


def test_metric_callback_exception_is_swallowed_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A raising metric callback is swallowed and logged with the metric name + error.

    Exercises the defensive ``except`` block and pins its warning's format string
    + both args (name, exc).
    """

    def _boom(name: str, value: int) -> None:
        raise ValueError("boom")

    store, _ = _store(on_metric=_boom)
    with caplog.at_level(logging.WARNING, logger="provide.uterm.server.bridge.hub"):
        store.metric("hits", 3)  # must not raise
    assert any(
        "metric_callback_failed metric=hits error=boom [provide.uterm.server.bridge.hub.store]" in r.getMessage()
        for r in caplog.records
    )


# == hijack-state predicates =================================================


@pytest.mark.parametrize(
    ("lease_s", "expected"),
    [(0, 1), (-10, 1), (50, 50), (14_400, 14_400), (20_000, 14_400)],
)
def test_clamp_lease_bounds(lease_s: int, expected: int) -> None:
    assert StateStore.clamp_lease(lease_s) == expected


def test_has_valid_rest_lease_variants() -> None:
    # No session → False.
    assert StateStore.has_valid_rest_lease(WorkerTermState()) is False
    # Future lease → True.
    st_future = WorkerTermState()
    st_future.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 100)
    assert StateStore.has_valid_rest_lease(st_future) is True
    # Expired lease → False (pins the ``>`` comparison).
    st_past = WorkerTermState()
    st_past.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() - 100)
    assert StateStore.has_valid_rest_lease(st_past) is False


def test_has_valid_rest_lease_expires_at_boundary_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_module.time, "monotonic", lambda: 100.0)
    st = WorkerTermState()
    st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=100.0)

    assert StateStore.has_valid_rest_lease(st) is False


def test_is_dashboard_hijack_active_variants() -> None:
    # No owner → False.
    assert StateStore.is_dashboard_hijack_active(WorkerTermState()) is False
    # Owner with no expiry → True (perpetual).
    st_perpetual = WorkerTermState()
    st_perpetual.hijack_owner = object()
    st_perpetual.hijack_owner_expires_at = None
    assert StateStore.is_dashboard_hijack_active(st_perpetual) is True
    # Owner with future expiry → True.
    st_future = WorkerTermState()
    st_future.hijack_owner = object()
    st_future.hijack_owner_expires_at = time.monotonic() + 100
    assert StateStore.is_dashboard_hijack_active(st_future) is True
    # Owner with past expiry → False (pins the ``>``).
    st_past = WorkerTermState()
    st_past.hijack_owner = object()
    st_past.hijack_owner_expires_at = time.monotonic() - 100
    assert StateStore.is_dashboard_hijack_active(st_past) is False


def test_dashboard_hijack_expiry_boundary_is_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_module.time, "monotonic", lambda: 200.0)
    st = WorkerTermState()
    st.hijack_owner = object()
    st.hijack_owner_expires_at = 200.0

    assert StateStore.is_dashboard_hijack_active(st) is False


def test_is_hijacked_is_logical_or() -> None:
    store, _ = _store()
    # Dashboard active only → True.
    st_dash = WorkerTermState()
    st_dash.hijack_owner = object()
    st_dash.hijack_owner_expires_at = None
    assert store.is_hijacked(st_dash) is True
    # REST lease only → True.
    st_rest = WorkerTermState()
    st_rest.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 100)
    assert store.is_hijacked(st_rest) is True
    # Neither → False.
    assert store.is_hijacked(WorkerTermState()) is False


# == notify_hijack_changed (sync paths) ======================================


def test_notify_hijack_changed_no_callback_is_noop() -> None:
    store, _ = _store(on_hijack_changed=None)
    store.notify_hijack_changed("w", enabled=True, owner="me")  # must not raise


def test_notify_hijack_changed_sync_callback_receives_args() -> None:
    seen: list[tuple[str, bool, str | None]] = []
    store, _ = _store(on_hijack_changed=lambda wid, enabled, owner: seen.append((wid, enabled, owner)))
    store.notify_hijack_changed("w", enabled=True, owner="me")
    assert seen == [("w", True, "me")]


async def test_notify_hijack_changed_async_exception_logs_worker_and_error(caplog: pytest.LogCaptureFixture) -> None:
    """A raising async callback logs a warning naming the worker_id + the error.

    Pins the done-callback warning's two args (worker_id, t.exception()) and its
    format string — the polling coverage suite only checks the prefix.
    """

    async def _boom(wid: str, enabled: bool, owner: str | None) -> None:
        raise ValueError("kapow")

    store, _ = _store(on_hijack_changed=_boom)
    with caplog.at_level(logging.WARNING, logger="provide.uterm.server.bridge.hub"):
        store.notify_hijack_changed("w", enabled=True, owner="me")
        await asyncio.sleep(0.05)  # let the fire-and-forget task + done-callback run
    # Anchored on the logger suffix to pin the worker_id + error args AND the
    # event text against an "XX…XX"-wrapped string mutant.
    assert any(
        "on_hijack_changed callback raised worker_id=w error=kapow [provide.uterm.server.bridge.hub.store]" in m
        for m in (r.getMessage() for r in caplog.records)
    )
