#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for the owned-send surface of :class:`HijackLeaseManager`.

Four methods — ``send_owned_worker``, ``run_owned_browser_operation``,
``capture_browser_ownership`` and ``capture_dashboard_ownership`` — had **no
bound test at all** in the mutmut selection: every one of their mutants sat in
the ``"no tests"`` state, which counts toward the denominator and so held the
file below 100 regardless of how many survivors were killed elsewhere.

They are the enforcement point for input ownership. Each one answers "may this
websocket / this REST lease actually type into the worker right now?", and a
mutation that flips an ``is not`` or drops a generation comparison converts a
rejection into an accepted keystroke from a party that no longer owns the
session. Every assertion below pins an exact value — the full return tuple, the
exact rejection reason string, the recorded ``expected_worker`` identity — so an
operator or literal mutation flips a concrete expectation.

Lease-extension deadlines are asserted as a tight interval bracketed by real
``time.monotonic()`` readings rather than by patching the clock: patching
``time.monotonic`` module-wide also perturbs the running event loop, and the
bracket is narrow enough (microseconds) to kill any TTL or sign mutation.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from provide.uterm.server.bridge.hub.lease import HijackLeaseManager
from provide.uterm.server.bridge.hub.registry import WorkerRegistry
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

_TTL = 45
_SEED_GENERATION = 7


class _FakeHub:
    """Minimal ``_LeaseHubCallbacks`` impl that records send_worker kwargs."""

    def __init__(self) -> None:
        self.sends: list[tuple[str, dict[str, Any], Any, Any]] = []
        self.send_worker_result = True
        self.send_worker_exc: BaseException | None = None

    def is_hijacked(self, st: WorkerTermState) -> bool:
        return self.is_dashboard_hijack_active(st) or self.has_valid_rest_lease(st)

    def is_dashboard_hijack_active(self, st: WorkerTermState) -> bool:
        return st.hijack_owner is not None and (
            st.hijack_owner_expires_at is None or st.hijack_owner_expires_at > time.monotonic()
        )

    def has_valid_rest_lease(self, st: WorkerTermState) -> bool:
        return st.hijack_session is not None and st.hijack_session.lease_expires_at > time.monotonic()

    def can_send_input(self, st: WorkerTermState, ws: Any) -> bool:
        return st.hijack_owner is ws or st.input_mode == "open"

    def metric(self, name: str, value: int = 1) -> None:
        return None

    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None = None) -> None:
        return None

    async def send_worker(
        self,
        worker_id: str,
        msg: dict[str, Any],
        *,
        source: Any = None,
        expected_worker: Any = None,
    ) -> bool:
        self.sends.append((worker_id, msg, source, expected_worker))
        if self.send_worker_exc is not None:
            raise self.send_worker_exc
        return self.send_worker_result

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        return None

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        return {}

    async def prune_if_idle(self, worker_id: str) -> None:
        return None

    async def _recheck_and_resume(self, worker_id: str, now: float) -> None:
        return None


class _ReplacingFence:
    """Fence that swaps the registry entry on entry (models a mid-wait reconnect)."""

    def __init__(self, registry: WorkerRegistry, worker_id: str, replacement: WorkerTermState) -> None:
        self._registry = registry
        self._worker_id = worker_id
        self._replacement = replacement

    async def __aenter__(self) -> None:
        self._registry.put(self._worker_id, self._replacement)

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _WaitForSpy:
    """Record the ``timeout`` of every ``asyncio.wait_for`` the module makes."""

    def __init__(self) -> None:
        self.timeouts: list[float | None] = []
        self._real = asyncio.wait_for

    async def __call__(self, awaitable: Any, timeout: float | None) -> Any:
        self.timeouts.append(timeout)
        return await self._real(awaitable, timeout)


def _make_state() -> WorkerTermState:
    st = WorkerTermState()
    st.worker_ws = AsyncMock()
    st.ownership_generation = _SEED_GENERATION
    return st


def _make_manager() -> tuple[HijackLeaseManager, WorkerRegistry, _FakeHub]:
    registry = WorkerRegistry()
    hub = _FakeHub()
    mgr = HijackLeaseManager(
        registry=registry,
        lock=asyncio.Lock(),
        dashboard_hijack_lease_s=_TTL,
        hub=hub,
    )
    return mgr, registry, hub


def _owned_by(ws: Any) -> WorkerTermState:
    """A state whose dashboard hijack is held by *ws*."""
    st = _make_state()
    st.hijack_owner = ws
    st.hijack_owner_expires_at = time.monotonic() + _TTL
    return st


def _rest_state(*, hijack_id: str = "hid", expires_in: float = 60.0) -> WorkerTermState:
    st = _make_state()
    now = time.monotonic()
    st.hijack_session = HijackSession(
        hijack_id=hijack_id,
        owner="op",
        acquired_at=now,
        lease_expires_at=now + expires_in,
        last_heartbeat=now,
    )
    return st


# =====================================================================
# send_owned_worker
# =====================================================================


class TestSendOwnedWorkerOwnerArgument:
    """Exactly one owner must be named — the two kinds are not interchangeable."""

    async def test_rejects_both_owners(self) -> None:
        mgr, registry, _hub = _make_manager()
        registry.put("w1", _make_state())
        with pytest.raises(ValueError) as excinfo:
            await mgr.send_owned_worker("w1", {}, browser_ws=AsyncMock(), rest_hijack_id="hid")
        assert str(excinfo.value) == "exactly one input owner must be specified"

    async def test_rejects_neither_owner(self) -> None:
        mgr, registry, _hub = _make_manager()
        registry.put("w1", _make_state())
        with pytest.raises(ValueError) as excinfo:
            await mgr.send_owned_worker("w1", {})
        assert str(excinfo.value) == "exactly one input owner must be specified"


class TestSendOwnedWorkerRejections:
    """Every rejection returns ``(False, <exact reason>)`` and sends nothing."""

    async def test_unknown_worker(self) -> None:
        mgr, _registry, hub = _make_manager()
        assert await mgr.send_owned_worker("ghost", {}, rest_hijack_id="hid") == (False, "invalid_owner")
        assert hub.sends == []

    async def test_state_replaced_during_fence(self) -> None:
        mgr, registry, hub = _make_manager()
        original = _rest_state()
        replacement = _rest_state()
        original.owned_input_fence = _ReplacingFence(registry, "w1", replacement)  # type: ignore[assignment]
        registry.put("w1", original)

        assert await mgr.send_owned_worker("w1", {}, rest_hijack_id="hid") == (False, "invalid_owner")
        assert hub.sends == []

    async def test_browser_without_input_permission(self) -> None:
        mgr, registry, hub = _make_manager()
        registry.put("w1", _owned_by(AsyncMock()))

        assert await mgr.send_owned_worker("w1", {}, browser_ws=AsyncMock()) == (False, "invalid_owner")
        assert hub.sends == []

    async def test_browser_with_stale_generation(self) -> None:
        mgr, registry, hub = _make_manager()
        ws = AsyncMock()
        registry.put("w1", _owned_by(ws))

        result = await mgr.send_owned_worker("w1", {}, browser_ws=ws, ownership_generation=_SEED_GENERATION - 1)

        assert result == (False, "invalid_owner")
        assert hub.sends == []

    async def test_rest_without_session(self) -> None:
        mgr, registry, hub = _make_manager()
        registry.put("w1", _make_state())

        assert await mgr.send_owned_worker("w1", {}, rest_hijack_id="hid") == (False, "invalid_owner")
        assert hub.sends == []

    async def test_rest_with_mismatched_hijack_id(self) -> None:
        mgr, registry, hub = _make_manager()
        registry.put("w1", _rest_state(hijack_id="mine"))

        assert await mgr.send_owned_worker("w1", {}, rest_hijack_id="theirs") == (False, "invalid_owner")
        assert hub.sends == []

    async def test_rest_with_lease_expired_exactly_now(self) -> None:
        """``<=`` not ``<``: a lease whose deadline has arrived is already gone.

        The clock is frozen for this one case because the boundary is only
        observable at exact equality — with a real clock, ``monotonic()`` has
        already moved past the deadline by the time the comparison runs, and
        ``<`` and ``<=`` agree. Nothing sleeps or times out inside the frozen
        window: the call is rejected before it reaches any bounded send.
        """
        mgr, registry, hub = _make_manager()
        deadline = 10_000.0
        st = _make_state()
        st.hijack_session = HijackSession(
            hijack_id="hid",
            owner="op",
            acquired_at=deadline,
            lease_expires_at=deadline,
            last_heartbeat=deadline,
        )
        registry.put("w1", st)

        with patch("provide.uterm.server.bridge.hub.lease.time.monotonic", return_value=deadline):
            result = await mgr.send_owned_worker("w1", {}, rest_hijack_id="hid")

        assert result == (False, "invalid_owner")
        assert hub.sends == []

    async def test_no_worker_socket(self) -> None:
        mgr, registry, hub = _make_manager()
        st = _rest_state()
        st.worker_ws = None
        registry.put("w1", st)

        assert await mgr.send_owned_worker("w1", {}, rest_hijack_id="hid") == (False, "no_worker")
        assert hub.sends == []


class TestSendOwnedWorkerDelivery:
    """The accepted path forwards the message pinned to the revalidated socket."""

    async def test_rest_owner_sends_and_returns_true_none(self) -> None:
        mgr, registry, hub = _make_manager()
        st = _rest_state()
        registry.put("w1", st)
        msg = {"type": "input", "data": "ls\n"}

        result = await mgr.send_owned_worker("w1", msg, rest_hijack_id="hid", source="rest")

        assert result == (True, None)
        assert hub.sends == [("w1", msg, "rest", st.worker_ws)]

    async def test_browser_owner_sends_with_matching_generation(self) -> None:
        mgr, registry, hub = _make_manager()
        ws = AsyncMock()
        st = _owned_by(ws)
        registry.put("w1", st)

        result = await mgr.send_owned_worker("w1", {"k": 1}, browser_ws=ws, ownership_generation=_SEED_GENERATION)

        assert result == (True, None)
        assert hub.sends == [("w1", {"k": 1}, None, st.worker_ws)]

    async def test_generation_none_skips_the_generation_check(self) -> None:
        """``ownership_generation=None`` means "don't care", not "must be None"."""
        mgr, registry, hub = _make_manager()
        ws = AsyncMock()
        registry.put("w1", _owned_by(ws))

        assert await mgr.send_owned_worker("w1", {}, browser_ws=ws) == (True, None)
        assert len(hub.sends) == 1

    async def test_dashboard_owner_send_extends_the_lease(self) -> None:
        mgr, registry, _hub = _make_manager()
        ws = AsyncMock()
        st = _owned_by(ws)
        st.hijack_owner_expires_at = time.monotonic() + 1  # live, but far short of a full TTL
        registry.put("w1", st)

        before = time.monotonic()
        await mgr.send_owned_worker("w1", {}, browser_ws=ws)
        after = time.monotonic()

        assert st.hijack_owner_expires_at is not None
        assert before + _TTL <= st.hijack_owner_expires_at <= after + _TTL

    async def test_open_mode_sender_does_not_extend_anyone_elses_lease(self) -> None:
        """Open input mode grants send rights without granting the hijack lease."""
        mgr, registry, _hub = _make_manager()
        owner = AsyncMock()
        st = _owned_by(owner)
        st.input_mode = "open"
        deadline = time.monotonic() + _TTL
        st.hijack_owner_expires_at = deadline
        registry.put("w1", st)

        assert await mgr.send_owned_worker("w1", {}, browser_ws=AsyncMock()) == (True, None)
        assert st.hijack_owner_expires_at == deadline

    async def test_send_failure_reports_no_worker(self) -> None:
        mgr, registry, hub = _make_manager()
        registry.put("w1", _rest_state())
        hub.send_worker_result = False

        assert await mgr.send_owned_worker("w1", {}, rest_hijack_id="hid") == (False, "no_worker")

    async def test_send_timeout_reports_no_worker(self) -> None:
        mgr, registry, hub = _make_manager()
        registry.put("w1", _rest_state())
        hub.send_worker_exc = TimeoutError()

        assert await mgr.send_owned_worker("w1", {}, rest_hijack_id="hid") == (False, "no_worker")

    async def test_send_is_bounded_by_the_owned_input_timeout(self) -> None:
        mgr, registry, _hub = _make_manager()
        registry.put("w1", _rest_state())
        spy = _WaitForSpy()

        with patch("provide.uterm.server.bridge.hub.lease.asyncio.wait_for", spy):
            assert await mgr.send_owned_worker("w1", {}, rest_hijack_id="hid") == (True, None)

        assert spy.timeouts == [5.0]


# =====================================================================
# run_owned_browser_operation
# =====================================================================


async def _echo_operation(send: Any) -> tuple[bool, str | None]:
    """Operation that performs one reserved send and reports its result."""
    ok = await send({"type": "input", "data": "y"})
    return ok, None if ok else "no_worker"


class TestRunOwnedBrowserOperationRejections:
    """Rejections return ``(None, <reason>)`` — the operation never runs."""

    async def test_unknown_worker(self) -> None:
        mgr, _registry, hub = _make_manager()
        result = await mgr.run_owned_browser_operation(
            "ghost", _echo_operation, browser_ws=AsyncMock(), ownership_generation=_SEED_GENERATION
        )
        assert result == (None, "invalid_owner")
        assert hub.sends == []

    async def test_state_replaced_during_fence(self) -> None:
        mgr, registry, hub = _make_manager()
        ws = AsyncMock()
        original = _owned_by(ws)
        original.owned_input_fence = _ReplacingFence(registry, "w1", _owned_by(ws))  # type: ignore[assignment]
        registry.put("w1", original)

        result = await mgr.run_owned_browser_operation(
            "w1", _echo_operation, browser_ws=ws, ownership_generation=_SEED_GENERATION
        )

        assert result == (None, "invalid_owner")
        assert hub.sends == []

    async def test_browser_without_input_permission(self) -> None:
        mgr, registry, hub = _make_manager()
        registry.put("w1", _owned_by(AsyncMock()))

        result = await mgr.run_owned_browser_operation(
            "w1", _echo_operation, browser_ws=AsyncMock(), ownership_generation=_SEED_GENERATION
        )

        assert result == (None, "invalid_owner")
        assert hub.sends == []

    async def test_stale_generation(self) -> None:
        mgr, registry, hub = _make_manager()
        ws = AsyncMock()
        registry.put("w1", _owned_by(ws))

        result = await mgr.run_owned_browser_operation(
            "w1", _echo_operation, browser_ws=ws, ownership_generation=_SEED_GENERATION + 1
        )

        assert result == (None, "invalid_owner")
        assert hub.sends == []

    async def test_no_worker_socket(self) -> None:
        mgr, registry, hub = _make_manager()
        ws = AsyncMock()
        st = _owned_by(ws)
        st.worker_ws = None
        registry.put("w1", st)

        result = await mgr.run_owned_browser_operation(
            "w1", _echo_operation, browser_ws=ws, ownership_generation=_SEED_GENERATION
        )

        assert result == (None, "no_worker")
        assert hub.sends == []


class TestRunOwnedBrowserOperationExecution:
    """The operation's own return value is passed through verbatim."""

    async def test_returns_operation_result_and_no_error(self) -> None:
        mgr, registry, hub = _make_manager()
        ws = AsyncMock()
        st = _owned_by(ws)
        registry.put("w1", st)

        result = await mgr.run_owned_browser_operation(
            "w1", _echo_operation, browser_ws=ws, ownership_generation=_SEED_GENERATION, source="approval"
        )

        assert result == ((True, None), None)
        assert hub.sends == [("w1", {"type": "input", "data": "y"}, "approval", st.worker_ws)]

    async def test_multiple_sends_stay_pinned_to_one_socket(self) -> None:
        """The reserved sender is bound to the socket present at revalidation.

        Approval delivery sends the command and then the replayed buffer; both
        must reach the same worker even if the socket is swapped in between.
        """
        mgr, registry, hub = _make_manager()
        ws = AsyncMock()
        st = _owned_by(ws)
        registry.put("w1", st)
        pinned = st.worker_ws

        async def two_sends(send: Any) -> tuple[bool, str | None]:
            first = await send({"n": 1})
            st.worker_ws = AsyncMock()  # a reconnect lands mid-operation
            second = await send({"n": 2})
            return first and second, None

        result = await mgr.run_owned_browser_operation(
            "w1", two_sends, browser_ws=ws, ownership_generation=_SEED_GENERATION
        )

        assert result == ((True, None), None)
        assert [call[3] for call in hub.sends] == [pinned, pinned]

    async def test_reserved_send_reports_false_on_timeout(self) -> None:
        mgr, registry, hub = _make_manager()
        ws = AsyncMock()
        registry.put("w1", _owned_by(ws))
        hub.send_worker_exc = TimeoutError()

        result = await mgr.run_owned_browser_operation(
            "w1", _echo_operation, browser_ws=ws, ownership_generation=_SEED_GENERATION
        )

        assert result == ((False, "no_worker"), None)

    async def test_reserved_send_is_bounded_by_the_owned_input_timeout(self) -> None:
        mgr, registry, _hub = _make_manager()
        ws = AsyncMock()
        registry.put("w1", _owned_by(ws))
        spy = _WaitForSpy()

        with patch("provide.uterm.server.bridge.hub.lease.asyncio.wait_for", spy):
            await mgr.run_owned_browser_operation(
                "w1", _echo_operation, browser_ws=ws, ownership_generation=_SEED_GENERATION
            )

        assert spy.timeouts == [5.0]

    async def test_open_mode_operator_does_not_renew_the_owners_lease(self) -> None:
        """Both halves of the guard matter: active hijack AND this exact socket.

        In open mode any browser may drive the operation, but only the hijack
        owner's own activity may push the owner's deadline out — otherwise a
        bystander's approval keeps someone else's lease alive indefinitely.
        """
        mgr, registry, _hub = _make_manager()
        owner = AsyncMock()
        st = _owned_by(owner)
        st.input_mode = "open"
        deadline = time.monotonic() + _TTL
        st.hijack_owner_expires_at = deadline
        registry.put("w1", st)

        result = await mgr.run_owned_browser_operation(
            "w1", _echo_operation, browser_ws=AsyncMock(), ownership_generation=_SEED_GENERATION
        )

        assert result == ((True, None), None)
        assert st.hijack_owner_expires_at == deadline

    async def test_dashboard_owner_lease_is_extended(self) -> None:
        mgr, registry, _hub = _make_manager()
        ws = AsyncMock()
        st = _owned_by(ws)
        st.hijack_owner_expires_at = time.monotonic() + 1
        registry.put("w1", st)

        before = time.monotonic()
        await mgr.run_owned_browser_operation(
            "w1", _echo_operation, browser_ws=ws, ownership_generation=_SEED_GENERATION
        )
        after = time.monotonic()

        assert st.hijack_owner_expires_at is not None
        assert before + _TTL <= st.hijack_owner_expires_at <= after + _TTL


# =====================================================================
# capture_browser_ownership / capture_dashboard_ownership
# =====================================================================


class TestCaptureBrowserOwnership:
    """Returns the generation a later send must still match, or ``None``."""

    async def test_unknown_worker_returns_none(self) -> None:
        mgr, _registry, _hub = _make_manager()
        assert await mgr.capture_browser_ownership("ghost", AsyncMock()) is None

    async def test_socket_without_input_permission_returns_none(self) -> None:
        mgr, registry, _hub = _make_manager()
        registry.put("w1", _owned_by(AsyncMock()))
        assert await mgr.capture_browser_ownership("w1", AsyncMock()) is None

    async def test_owner_gets_generation_and_a_renewed_lease(self) -> None:
        mgr, registry, _hub = _make_manager()
        ws = AsyncMock()
        st = _owned_by(ws)
        st.hijack_owner_expires_at = time.monotonic() + 1
        registry.put("w1", st)

        before = time.monotonic()
        generation = await mgr.capture_browser_ownership("w1", ws)
        after = time.monotonic()

        assert generation == _SEED_GENERATION
        assert st.hijack_owner_expires_at is not None
        assert before + _TTL <= st.hijack_owner_expires_at <= after + _TTL

    async def test_open_mode_non_owner_gets_generation_without_renewing(self) -> None:
        mgr, registry, _hub = _make_manager()
        owner = AsyncMock()
        st = _owned_by(owner)
        st.input_mode = "open"
        deadline = time.monotonic() + _TTL
        st.hijack_owner_expires_at = deadline
        registry.put("w1", st)

        assert await mgr.capture_browser_ownership("w1", AsyncMock()) == _SEED_GENERATION
        assert st.hijack_owner_expires_at == deadline


class TestCaptureDashboardOwnership:
    """Stricter than the browser variant: only the dashboard owner qualifies."""

    async def test_unknown_worker_returns_none(self) -> None:
        mgr, _registry, _hub = _make_manager()
        assert await mgr.capture_dashboard_ownership("ghost", AsyncMock()) is None

    async def test_no_active_dashboard_hijack_returns_none(self) -> None:
        mgr, registry, _hub = _make_manager()
        registry.put("w1", _make_state())
        assert await mgr.capture_dashboard_ownership("w1", AsyncMock()) is None

    async def test_open_mode_does_not_substitute_for_ownership(self) -> None:
        """``can_send_input`` is deliberately NOT consulted here."""
        mgr, registry, _hub = _make_manager()
        st = _owned_by(AsyncMock())
        st.input_mode = "open"
        registry.put("w1", st)

        assert await mgr.capture_dashboard_ownership("w1", AsyncMock()) is None

    async def test_owner_gets_generation_and_a_renewed_lease(self) -> None:
        mgr, registry, _hub = _make_manager()
        ws = AsyncMock()
        st = _owned_by(ws)
        st.hijack_owner_expires_at = time.monotonic() + 1
        registry.put("w1", st)

        before = time.monotonic()
        generation = await mgr.capture_dashboard_ownership("w1", ws)
        after = time.monotonic()

        assert generation == _SEED_GENERATION
        assert st.hijack_owner_expires_at is not None
        assert before + _TTL <= st.hijack_owner_expires_at <= after + _TTL
