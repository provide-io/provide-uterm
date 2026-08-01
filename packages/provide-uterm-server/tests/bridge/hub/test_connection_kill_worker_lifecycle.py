#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing unit tests for the worker-lifecycle methods of
:class:`provide.uterm.server.bridge.hub.connection.ConnectionManager`.

Targets ``register_worker``, ``deregister_worker`` and ``is_active_worker``.
Each test constructs a FRESH :class:`TermHub` and drives the methods through
``hub.connection_mgr`` (so the exact ``logger`` / ``tracer`` calls emitted by
*this* module are isolated from the facade's ``emit_telemetry`` wrapper) and,
where the public contract is the load-bearing thing, through the hub facade.

Every observable is pinned: exact return values (both tuple elements), every
mutated :class:`WorkerTermState` field (``worker_ws``, ``hijack_session``,
``hijack_owner``, ``hijack_owner_expires_at``, the rebuilt ``events`` deque
``maxlen``), the worker-cap rejection (``WebSocketException(1008)``), the
expired-vs-live lease boundary, and the exact span name/attributes +
structured ``logger.info`` event constant emitted on each path.

The ``TestRegisterWorkerOwnershipFencing`` / generation-pinning tests cover
the input-ownership fencing added by "fence lifecycle input ownership"
(61647de9) and the hello-mode work (662d72eb): ``ownership_generation``
transitions (fresh == 1, epoch-change +1, idempotent re-register unchanged),
the tunnel-flag plumbing on both branches, the fenced re-read retry loop
(bounded registry reads + the state-swap race), and the fenced
``deregister_worker`` early return.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocketException

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.hub.ext import EVENT_SESSION_REGISTERED
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

_LOGGER = "provide.uterm.server.bridge.hub.connection.logger"
_TRACER = "provide.uterm.server.bridge.hub.connection.tracer"

# register_worker's fenced re-read loop never suspends (uncontended asyncio.Lock
# acquires take the no-await fast path), so the register_worker__mutmut_40/_41/_42
# infinite-retry mutants spin without yielding. Whichever reconnect-path test runs
# first under those mutants must FAIL via a short SIGALRM bound (a recorded kill)
# BEFORE mutmut's wall-clock limit fires (which would record a bad `timeout`
# instead). The marker overrides the mutation run's --timeout=90.
pytestmark = pytest.mark.timeout(10)


def _ws() -> MagicMock:
    """A MagicMock WebSocket with the awaitable methods the hub may touch."""
    ws = MagicMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    return ws


async def _put_state(hub: TermHub, worker_id: str, st: WorkerTermState) -> None:
    async with hub._lock:
        hub.registry._workers[worker_id] = st


def _hijack(owner: str = "alice", *, lease_expires_at: float) -> HijackSession:
    return HijackSession(
        hijack_id="hid",
        owner=owner,
        lease_expires_at=lease_expires_at,
        acquired_at=0.0,
        last_heartbeat=0.0,
    )


# ===========================================================================
# register_worker
# ===========================================================================


class TestRegisterWorker:
    """``register_worker`` cap gate, lease clearing, state mutation, telemetry."""

    async def test_new_worker_sets_ws_and_returns_false(self) -> None:
        """A brand-new worker_id: returns False (no prior hijack), st.worker_ws set."""
        hub = TermHub()
        ws = _ws()

        result = await hub.connection_mgr.register_worker("w-new", ws)

        assert result is False
        async with hub._lock:
            st = hub.registry._workers["w-new"]
        assert st.worker_ws is ws
        assert st.hijack_session is None
        assert st.hijack_owner is None
        assert st.hijack_owner_expires_at is None

    async def test_events_deque_rebuilt_with_hub_maxlen(self) -> None:
        """The events deque is rebuilt with maxlen == hub._event_deque_maxlen."""
        hub = TermHub(event_deque_maxlen=7)
        st = WorkerTermState()
        # Seed with a plain list to prove the rebuild produces a bounded deque.
        st.events = deque([{"type": "x"}])  # type: ignore[arg-type]
        await _put_state(hub, "w-deque", st)

        await hub.connection_mgr.register_worker("w-deque", _ws())

        async with hub._lock:
            rebuilt = hub.registry._workers["w-deque"].events
        assert isinstance(rebuilt, deque)
        assert rebuilt.maxlen == 7
        # The prior contents are preserved through the rebuild.
        assert list(rebuilt) == [{"type": "x"}]

    async def test_reconnecting_worker_id_allowed_even_at_cap(self) -> None:
        """A reconnecting (already-registered) worker_id is allowed at capacity."""
        hub = TermHub(max_workers=1)
        existing = WorkerTermState()
        await _put_state(hub, "w-recon", existing)

        # Map is full (len == max_workers == 1) but worker_id is already present.
        new_ws = _ws()
        result = await hub.connection_mgr.register_worker("w-recon", new_ws)

        assert result is False
        async with hub._lock:
            st = hub.registry._workers["w-recon"]
        # Same state object reused (setdefault), ws swapped to the reconnecting one.
        assert st is existing
        assert st.worker_ws is new_ws

    async def test_new_worker_id_rejected_at_cap_with_1008(self) -> None:
        """A NEW worker_id at capacity raises WebSocketException(1008) and adds nothing."""
        hub = TermHub(max_workers=1)
        await _put_state(hub, "w-existing", WorkerTermState())

        with pytest.raises(WebSocketException) as ei:
            await hub.connection_mgr.register_worker("w-overflow", _ws())

        assert ei.value.code == 1008
        assert ei.value.reason == "worker capacity exceeded"
        # The rejected worker_id never entered the map.
        async with hub._lock:
            assert "w-overflow" not in hub.registry._workers
            assert len(hub.registry._workers) == 1

    async def test_cap_boundary_is_ge_not_gt(self) -> None:
        """At exactly max_workers a new id is rejected (>=), proving the boundary.

        With max_workers=2 and one worker present (len=1 < 2) a new id is
        accepted; with two present (len=2 >= 2) a third new id is rejected.
        """
        hub = TermHub(max_workers=2)
        await _put_state(hub, "w1", WorkerTermState())

        # len == 1 < 2 -> accepted.
        assert await hub.connection_mgr.register_worker("w2", _ws()) is False
        # len == 2 >= 2 -> a third NEW id is rejected.
        with pytest.raises(WebSocketException) as ei:
            await hub.connection_mgr.register_worker("w3", _ws())
        assert ei.value.code == 1008

    async def test_expired_lease_cleared_and_returns_true(self) -> None:
        """An expired REST lease is cleared; prev_was_hijacked is True."""
        hub = TermHub()
        now = time.monotonic()
        st = WorkerTermState()
        st.hijack_session = _hijack(lease_expires_at=now - 1.0)
        st.hijack_owner = MagicMock()
        st.hijack_owner_expires_at = now - 1.0
        await _put_state(hub, "w-exp", st)

        ws = _ws()
        result = await hub.connection_mgr.register_worker("w-exp", ws)

        assert result is True
        async with hub._lock:
            st = hub.registry._workers["w-exp"]
        assert st.hijack_session is None
        assert st.hijack_owner is None
        assert st.hijack_owner_expires_at is None
        assert st.worker_ws is ws

    async def test_live_lease_preserved_and_returns_false(self) -> None:
        """A live REST lease (lease_expires_at > now) survives a reconnect."""
        hub = TermHub()
        now = time.monotonic()
        st = WorkerTermState()
        live = _hijack(lease_expires_at=now + 60.0)
        st.hijack_session = live
        # No dashboard owner -> the (session is None and owner is not None) arm is False.
        st.hijack_owner = None
        st.hijack_owner_expires_at = None
        await _put_state(hub, "w-live", st)

        ws = _ws()
        result = await hub.connection_mgr.register_worker("w-live", ws)

        assert result is False
        async with hub._lock:
            st = hub.registry._workers["w-live"]
        # Session is NOT cleared because the lease is still valid.
        assert st.hijack_session is live
        assert st.worker_ws is ws

    async def test_lease_boundary_is_le_now(self, monkeypatch: Any) -> None:
        """lease_expires_at == now counts as expired (<=), so it IS cleared.

        Freeze time.monotonic in the connection module and set the lease to
        exactly that value. A ``< now`` mutant would (wrongly) preserve it.
        """
        hub = TermHub()
        frozen = 5000.0
        monkeypatch.setattr("provide.uterm.server.bridge.hub.connection.time.monotonic", lambda: frozen)
        st = WorkerTermState()
        st.hijack_session = _hijack(lease_expires_at=frozen)
        await _put_state(hub, "w-boundary", st)

        result = await hub.connection_mgr.register_worker("w-boundary", _ws())

        assert result is True
        async with hub._lock:
            assert hub.registry._workers["w-boundary"].hijack_session is None

    async def test_lease_one_tick_above_now_preserved(self, monkeypatch: Any) -> None:
        """lease_expires_at strictly above now is preserved (anchors the <= direction)."""
        hub = TermHub()
        frozen = 5000.0
        monkeypatch.setattr("provide.uterm.server.bridge.hub.connection.time.monotonic", lambda: frozen)
        st = WorkerTermState()
        live = _hijack(lease_expires_at=frozen + 0.001)
        st.hijack_session = live
        st.hijack_owner = None
        await _put_state(hub, "w-above", st)

        result = await hub.connection_mgr.register_worker("w-above", _ws())

        assert result is False
        async with hub._lock:
            assert hub.registry._workers["w-above"].hijack_session is live

    async def test_dashboard_owner_without_session_returns_true_and_clears_owner(self) -> None:
        """No session but a live dashboard owner -> prev_was_hijacked True, owner cleared."""
        hub = TermHub()
        st = WorkerTermState()
        st.hijack_session = None
        st.hijack_owner = MagicMock()
        st.hijack_owner_expires_at = time.monotonic() + 60.0
        await _put_state(hub, "w-dash", st)

        result = await hub.connection_mgr.register_worker("w-dash", _ws())

        assert result is True
        async with hub._lock:
            st = hub.registry._workers["w-dash"]
        assert st.hijack_owner is None
        assert st.hijack_owner_expires_at is None
        # Session was already None; it stays None.
        assert st.hijack_session is None

    async def test_expired_session_but_owner_none_still_clears_owner_fields(self) -> None:
        """prev_was_hijacked from an expired session also clears the owner fields.

        Pins that the ``if prev_was_hijacked:`` block (clearing owner +
        owner_expires_at) runs on the expired-session arm, not only on the
        owner-present arm.
        """
        hub = TermHub()
        now = time.monotonic()
        st = WorkerTermState()
        st.hijack_session = _hijack(lease_expires_at=now - 5.0)
        owner = MagicMock()
        st.hijack_owner = owner
        st.hijack_owner_expires_at = now + 100.0
        await _put_state(hub, "w-both", st)

        result = await hub.connection_mgr.register_worker("w-both", _ws())

        assert result is True
        async with hub._lock:
            st = hub.registry._workers["w-both"]
        assert st.hijack_session is None
        assert st.hijack_owner is None
        assert st.hijack_owner_expires_at is None

    async def test_opens_exact_register_span_and_logs_event(self) -> None:
        """Pins the span name + attributes and the exact EVENT_SESSION_REGISTERED log."""
        hub = TermHub()
        ws = _ws()

        with patch(_LOGGER) as mlog, patch(_TRACER) as mtr:
            result = await hub.connection_mgr.register_worker("w-span", ws)

        assert result is False
        mtr.start_as_current_span.assert_called_once_with("uterm.worker.register", attributes={"worker_id": "w-span"})
        mlog.info.assert_called_once_with(EVENT_SESSION_REGISTERED, worker_id="w-span", session_type="worker")
        mlog.warning.assert_not_called()

    async def test_cap_rejection_does_not_log_registered_event(self) -> None:
        """On the 1008 rejection path no session-registered log is emitted."""
        hub = TermHub(max_workers=1)
        await _put_state(hub, "w-full", WorkerTermState())

        with patch(_LOGGER) as mlog, patch(_TRACER) as mtr:
            with pytest.raises(WebSocketException):
                await hub.connection_mgr.register_worker("w-reject", _ws())

        # Span is opened (it wraps the whole body) but the success log never fires.
        mtr.start_as_current_span.assert_called_once_with("uterm.worker.register", attributes={"worker_id": "w-reject"})
        mlog.info.assert_not_called()


class _BudgetedRegistry:
    """Registry proxy that fails the test if ``get`` is called past a budget.

    ``register_worker``'s fenced re-read loop must terminate after exactly two
    ``registry.get(worker_id)`` reads on an uncontended reconnect. A mutant
    that corrupts the re-read (``st = None``, ``get(None)``) or inverts the
    retry condition (``if st is state: continue``) loops forever WITHOUT ever
    suspending (the uncontended locks take their fast path), so a wall-clock
    timeout can never cancel it — the budget raise is the only deterministic
    way to surface the loop as a test failure.
    """

    def __init__(self, real: Any, budget: int = 8) -> None:
        self._real = real
        self._workers = real._workers
        self.get_calls: list[Any] = []
        self._budget = budget

    def get(self, worker_id: Any) -> Any:
        self.get_calls.append(worker_id)
        if len(self.get_calls) > self._budget:
            raise RuntimeError("register_worker is looping: registry.get exceeded its call budget")
        return self._real.get(worker_id)


class TestRegisterWorkerOwnershipFencing:
    """``register_worker`` generation transitions, tunnel flag, fenced retry loop."""

    async def test_new_worker_generation_starts_at_one(self) -> None:
        """A fresh registration seeds ownership_generation to exactly 1.

        Kills register_worker__mutmut_33 (``= None``) and __mutmut_34 (``= 2``).
        """
        hub = TermHub()
        await hub.connection_mgr.register_worker("w-gen1", _ws())
        async with hub._lock:
            assert hub.registry._workers["w-gen1"].ownership_generation == 1

    async def test_new_worker_default_tunnel_flag_is_false(self) -> None:
        """Calling without the keyword registers a non-tunnel worker.

        Kills register_worker__mutmut_1 (default flipped to ``True``).
        """
        hub = TermHub()
        await hub.connection_mgr.register_worker("w-notunnel", _ws())
        async with hub._lock:
            assert hub.registry._workers["w-notunnel"].is_tunnel_worker is False

    async def test_new_worker_tunnel_flag_true_reaches_state(self) -> None:
        """``is_tunnel_worker=True`` lands on the freshly constructed state.

        Kills register_worker__mutmut_25 (ctor kwarg -> ``None``) and
        __mutmut_27 (ctor kwarg dropped -> default ``False``).
        """
        hub = TermHub()
        await hub.connection_mgr.register_worker("w-tunnel", _ws(), is_tunnel_worker=True)
        async with hub._lock:
            assert hub.registry._workers["w-tunnel"].is_tunnel_worker is True

    async def test_new_worker_events_rebuild_preserves_contents_and_maxlen(self) -> None:
        """The new-state events rebuild keeps contents AND applies the hub maxlen.

        The state is constructed by the method itself, so a factory shim seeds
        one event at construction time to make the contents-copy observable.
        Kills register_worker__mutmut_28 (``events = None``), __mutmut_30
        (``maxlen=None``), __mutmut_31 (``state.events`` operand dropped — the
        seeded event would be lost) and __mutmut_32 (``maxlen`` kwarg dropped).
        """
        hub = TermHub(event_deque_maxlen=7)

        def _seeded_state(**kwargs: Any) -> WorkerTermState:
            st = WorkerTermState(**kwargs)
            st.events.append({"type": "seeded"})
            return st

        with patch("provide.uterm.server.bridge.hub.connection.WorkerTermState", side_effect=_seeded_state):
            await hub.connection_mgr.register_worker("w-seed", _ws())

        async with hub._lock:
            rebuilt = hub.registry._workers["w-seed"].events
        assert isinstance(rebuilt, deque)
        assert rebuilt.maxlen == 7
        assert list(rebuilt) == [{"type": "seeded"}]

    async def test_reconnect_new_ws_bumps_generation_once(self) -> None:
        """A reconnect with a NEW ws (no hijack) advances the epoch by exactly 1.

        Kills register_worker__mutmut_59 (``changed_epoch = None`` — no bump),
        __mutmut_60 (``or`` -> ``and`` — no bump without a hijack), __mutmut_67
        (``= 1``), __mutmut_68 (``-= 1``) and __mutmut_69 (``+= 2``).
        """
        hub = TermHub()
        st = WorkerTermState()
        st.worker_ws = _ws()
        st.ownership_generation = 5
        await _put_state(hub, "w-epoch", st)

        result = await hub.connection_mgr.register_worker("w-epoch", _ws())

        assert result is False
        async with hub._lock:
            assert hub.registry._workers["w-epoch"].ownership_generation == 6

    async def test_reregister_same_ws_no_hijack_keeps_generation(self) -> None:
        """Re-registering the CURRENT ws without any hijack does not bump the epoch.

        Kills register_worker__mutmut_61 (``is not ws`` -> ``is ws`` — would
        bump on the idempotent re-register).
        """
        hub = TermHub()
        ws = _ws()
        st = WorkerTermState()
        st.worker_ws = ws
        st.ownership_generation = 5
        await _put_state(hub, "w-idem", st)

        result = await hub.connection_mgr.register_worker("w-idem", ws)

        assert result is False
        async with hub._lock:
            assert hub.registry._workers["w-idem"].ownership_generation == 5

    async def test_reregister_same_ws_after_expired_lease_bumps_generation(self) -> None:
        """Same ws but an expired lease (prev_was_hijacked) still changes the epoch.

        Pins the ``or prev_was_hijacked`` arm on its own: kills
        register_worker__mutmut_60 (``or`` -> ``and``) even when the ws is
        unchanged.
        """
        hub = TermHub()
        ws = _ws()
        st = WorkerTermState()
        st.worker_ws = ws
        st.hijack_session = _hijack(lease_expires_at=time.monotonic() - 1.0)
        st.ownership_generation = 5
        await _put_state(hub, "w-samews", st)

        result = await hub.connection_mgr.register_worker("w-samews", ws)

        assert result is True
        async with hub._lock:
            assert hub.registry._workers["w-samews"].ownership_generation == 6

    async def test_reconnect_applies_tunnel_flag_to_existing_state(self) -> None:
        """The reconnect branch writes the passed tunnel flag onto the state.

        Kills register_worker__mutmut_66 (``st.is_tunnel_worker = None``).
        """
        hub = TermHub()
        st = WorkerTermState()
        st.worker_ws = _ws()
        await _put_state(hub, "w-retunnel", st)

        await hub.connection_mgr.register_worker("w-retunnel", _ws(), is_tunnel_worker=True)

        async with hub._lock:
            assert hub.registry._workers["w-retunnel"].is_tunnel_worker is True

    async def test_reconnect_reads_registry_exactly_twice_and_terminates(self) -> None:
        """An uncontended reconnect re-reads the registry exactly twice, by id.

        Kills the three infinite-loop (timeout) mutants: register_worker
        __mutmut_40 (``st = None`` — retry forever), __mutmut_41
        (``get(None)`` — never finds the state) and __mutmut_42 (``is not`` ->
        ``is`` — the successful re-read retries forever). Each spins without
        suspending, so the budgeted registry proxy converts the loop into a
        deterministic RuntimeError.
        """
        hub = TermHub()
        ws_new = _ws()
        st = WorkerTermState()
        st.worker_ws = _ws()
        await _put_state(hub, "w-loop", st)
        proxy = _BudgetedRegistry(hub.registry)
        hub.registry = proxy  # type: ignore[assignment]

        result = await hub.connection_mgr.register_worker("w-loop", ws_new)

        assert result is False
        assert proxy.get_calls == ["w-loop", "w-loop"]
        assert st.worker_ws is ws_new

    async def test_registry_swap_mid_fence_retries_onto_new_state(self) -> None:
        """A state swap during the fence wait retries and registers on the NEW state.

        Kills register_worker__mutmut_43 (``continue`` -> ``break`` — the loop
        would exit with ``prev_was_hijacked`` unbound and raise
        UnboundLocalError instead of retrying).
        """
        hub = TermHub()
        ws = _ws()
        original = WorkerTermState()
        original.worker_ws = _ws()
        replacement = WorkerTermState()

        class _SwappingFence:
            async def __aenter__(self) -> None:
                hub.registry._workers["w-swap"] = replacement

            async def __aexit__(self, *_args: Any) -> None:
                return None

        original.owned_input_fence = _SwappingFence()  # type: ignore[assignment]
        await _put_state(hub, "w-swap", original)

        result = await hub.connection_mgr.register_worker("w-swap", ws)

        assert result is False
        # The retry landed on the replacement state; the stale one is untouched.
        assert replacement.worker_ws is ws
        assert replacement.ownership_generation == 1
        assert original.worker_ws is not ws


# ===========================================================================
# deregister_worker
# ===========================================================================


class TestDeregisterWorker:
    """``deregister_worker`` current-ws gate, state clearing, span, return tuple."""

    async def test_current_ws_with_hijack_returns_true_true_and_clears(self) -> None:
        """Current worker ws + active hijack -> (True, True); all hijack fields cleared."""
        hub = TermHub()
        ws = _ws()
        st = WorkerTermState()
        st.worker_ws = ws
        st.hijack_session = _hijack(lease_expires_at=time.monotonic() + 60.0)
        st.hijack_owner = MagicMock()
        st.hijack_owner_expires_at = time.monotonic() + 60.0
        await _put_state(hub, "w-dc", st)

        should_broadcast, was_hijacked = await hub.connection_mgr.deregister_worker("w-dc", ws)

        assert should_broadcast is True
        assert was_hijacked is True
        async with hub._lock:
            st = hub.registry._workers["w-dc"]
        assert st.worker_ws is None
        assert st.hijack_session is None
        assert st.hijack_owner is None
        assert st.hijack_owner_expires_at is None

    async def test_current_ws_without_hijack_returns_true_false(self) -> None:
        """Current ws but no hijack -> (True, False); worker_ws cleared to None."""
        hub = TermHub()
        ws = _ws()
        st = WorkerTermState()
        st.worker_ws = ws
        st.hijack_session = None
        st.hijack_owner = None
        await _put_state(hub, "w-nh", st)

        should_broadcast, was_hijacked = await hub.connection_mgr.deregister_worker("w-nh", ws)

        assert should_broadcast is True
        assert was_hijacked is False
        async with hub._lock:
            assert hub.registry._workers["w-nh"].worker_ws is None

    async def test_was_hijacked_true_from_owner_only(self) -> None:
        """was_hijacked is True when only hijack_owner is set (session None)."""
        hub = TermHub()
        ws = _ws()
        st = WorkerTermState()
        st.worker_ws = ws
        st.hijack_session = None
        st.hijack_owner = MagicMock()
        await _put_state(hub, "w-owner", st)

        should_broadcast, was_hijacked = await hub.connection_mgr.deregister_worker("w-owner", ws)

        assert should_broadcast is True
        assert was_hijacked is True

    async def test_was_hijacked_true_from_session_only(self) -> None:
        """was_hijacked is True when only hijack_session is set (owner None)."""
        hub = TermHub()
        ws = _ws()
        st = WorkerTermState()
        st.worker_ws = ws
        st.hijack_session = _hijack(lease_expires_at=time.monotonic() + 60.0)
        st.hijack_owner = None
        await _put_state(hub, "w-sess", st)

        should_broadcast, was_hijacked = await hub.connection_mgr.deregister_worker("w-sess", ws)

        assert should_broadcast is True
        assert was_hijacked is True

    async def test_unknown_worker_returns_false_false(self) -> None:
        """st is None -> (False, False); no state mutation possible."""
        hub = TermHub()
        should_broadcast, was_hijacked = await hub.connection_mgr.deregister_worker("ghost", _ws())
        assert should_broadcast is False
        assert was_hijacked is False

    async def test_stale_ws_not_current_returns_false_false_without_clearing(self) -> None:
        """A non-current ws (replacement already took over) -> (False, False), no clears."""
        hub = TermHub()
        current_ws = _ws()
        stale_ws = _ws()
        st = WorkerTermState()
        st.worker_ws = current_ws
        sess = _hijack(lease_expires_at=time.monotonic() + 60.0)
        owner = MagicMock()
        st.hijack_session = sess
        st.hijack_owner = owner
        st.hijack_owner_expires_at = 123.0
        await _put_state(hub, "w-stale", st)

        should_broadcast, was_hijacked = await hub.connection_mgr.deregister_worker("w-stale", stale_ws)

        assert should_broadcast is False
        assert was_hijacked is False
        # Nothing cleared: the replacement worker's state is intact.
        async with hub._lock:
            st = hub.registry._workers["w-stale"]
        assert st.worker_ws is current_ws
        assert st.hijack_session is sess
        assert st.hijack_owner is owner
        assert st.hijack_owner_expires_at == 123.0

    async def test_opens_exact_deregister_span(self) -> None:
        """Pins the span name + attributes for the deregister path."""
        hub = TermHub()
        ws = _ws()
        st = WorkerTermState()
        st.worker_ws = ws
        await _put_state(hub, "w-span-dc", st)

        with patch(_TRACER) as mtr:
            result = await hub.connection_mgr.deregister_worker("w-span-dc", ws)

        assert result == (True, False)
        mtr.start_as_current_span.assert_called_once_with(
            "uterm.worker.deregister", attributes={"worker_id": "w-span-dc"}
        )

    async def test_span_opened_even_on_not_current_path(self) -> None:
        """The span wraps the whole body, so it opens even on the (False, False) early return."""
        hub = TermHub()
        st = WorkerTermState()
        st.worker_ws = _ws()
        await _put_state(hub, "w-span-stale", st)

        with patch(_TRACER) as mtr:
            result = await hub.connection_mgr.deregister_worker("w-span-stale", _ws())

        assert result == (False, False)
        mtr.start_as_current_span.assert_called_once_with(
            "uterm.worker.deregister", attributes={"worker_id": "w-span-stale"}
        )

    async def test_deregister_bumps_generation_once(self) -> None:
        """Clearing the current worker advances ownership_generation by exactly 1.

        Kills deregister_worker__mutmut_34 (``= 1``), __mutmut_35 (``-= 1``)
        and __mutmut_36 (``+= 2``).
        """
        hub = TermHub()
        ws = _ws()
        st = WorkerTermState()
        st.worker_ws = ws
        st.ownership_generation = 5
        await _put_state(hub, "w-degen", st)

        result = await hub.connection_mgr.deregister_worker("w-degen", ws)

        assert result == (True, False)
        async with hub._lock:
            assert hub.registry._workers["w-degen"].ownership_generation == 6

    async def test_ws_swapped_during_fence_wait_returns_false_false_untouched(self) -> None:
        """A ws replacement landing during the fence wait aborts the deregister.

        The fenced re-read sees the SAME state object but a replaced
        ``worker_ws``; the stale disconnect must return ``(False, False)`` and
        leave the replacement's ownership intact. Kills
        deregister_worker__mutmut_21 (``or`` -> ``and`` — would clear the
        replacement worker), __mutmut_24 (fenced return -> ``(True, False)``)
        and __mutmut_25 (fenced return -> ``(False, True)``).
        """
        hub = TermHub()
        ws_old = _ws()
        ws_new = _ws()
        st = WorkerTermState()
        st.worker_ws = ws_old
        st.hijack_owner = MagicMock()
        st.hijack_owner_expires_at = 123.0
        st.ownership_generation = 5

        class _WsSwappingFence:
            async def __aenter__(self) -> None:
                st.worker_ws = ws_new

            async def __aexit__(self, *_args: Any) -> None:
                return None

        st.owned_input_fence = _WsSwappingFence()  # type: ignore[assignment]
        await _put_state(hub, "w-fence", st)

        result = await hub.connection_mgr.deregister_worker("w-fence", ws_old)

        assert result == (False, False)
        # Nothing cleared: the replacement worker's ownership epoch is intact.
        assert st.worker_ws is ws_new
        assert st.hijack_owner is not None
        assert st.hijack_owner_expires_at == 123.0
        assert st.ownership_generation == 5


# ===========================================================================
# is_active_worker
# ===========================================================================


class TestIsActiveWorker:
    """``is_active_worker`` identity check against the registered worker ws."""

    async def test_returns_true_on_identity_match(self) -> None:
        hub = TermHub()
        ws = _ws()
        st = WorkerTermState()
        st.worker_ws = ws
        await _put_state(hub, "w-act", st)

        assert await hub.connection_mgr.is_active_worker("w-act", ws) is True

    async def test_returns_false_on_ws_mismatch(self) -> None:
        hub = TermHub()
        st = WorkerTermState()
        st.worker_ws = _ws()
        await _put_state(hub, "w-mis", st)

        assert await hub.connection_mgr.is_active_worker("w-mis", _ws()) is False

    async def test_returns_false_for_unknown_worker(self) -> None:
        hub = TermHub()
        assert await hub.connection_mgr.is_active_worker("ghost", _ws()) is False

    async def test_returns_false_when_worker_ws_is_none(self) -> None:
        """A registered worker with no active ws (worker_ws None) is not active."""
        hub = TermHub()
        st = WorkerTermState()
        st.worker_ws = None
        await _put_state(hub, "w-none", st)

        assert await hub.connection_mgr.is_active_worker("w-none", _ws()) is False
