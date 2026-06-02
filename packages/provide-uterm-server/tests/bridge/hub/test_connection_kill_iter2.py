#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Residual mutation-killing tests for ConnectionManager (iteration 2).

Targets the *residual* surviving mutmut mutants on
``provide.uterm.server.bridge.hub.connection.ConnectionManager`` that the
sibling kill-suites
(``test_connection_kill_register_browser.py``,
``test_connection_kill_worker_lifecycle.py``,
``test_connection_kill_worker_state.py``,
``test_connection_kill_browser_disconnect.py``,
``test_connection_kill_hijack_clearing.py``,
``test_connection_kill_ratelimit_force.py``)
do not already cover.

One focused test (group) per surviving mutant family:

1. ``set_worker_hello`` legacy-protocol boundary ``protocol_version < 1``
   (mutants flip to ``<= 1`` / ``< 2``).
2. ``register_browser`` default param ``defer_broadcast: bool = False``
   (mutant flips default to ``True``).
3. ``_update_lock_state`` ``st.browsers.pop(ws, None)`` graceful absent-key pop
   (mutant drops the ``None`` default -> ``KeyError``).
4. ``_rollback_browser_quota`` defensive ``.get(..., 0)`` / ``.pop(..., None)``
   defaults under inconsistent state (mutants -> ``None``/no-default raises).
5. ``_update_lock_state`` per-principal count defensive ``.get(..., 0)`` /
   ``.pop(..., None)`` defaults under inconsistent state.
6. ``cleanup_browser_disconnect`` ``task.add_done_callback(hub._background_tasks.discard)``
   (mutant -> ``add_done_callback(None)``).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import WorkerTermState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ws() -> MagicMock:
    """A browser/worker WebSocket double with the async surface the hub touches."""
    ws = MagicMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    ws.state = MagicMock()
    ws.state.uterm_principal = None
    return ws


async def _put(hub: TermHub, worker_id: str, st: WorkerTermState) -> None:
    async with hub._lock:
        hub._workers[worker_id] = st


# ===========================================================================
# Group 1 — set_worker_hello legacy-protocol boundary  (`protocol_version < 1`)
# ===========================================================================


class TestSetWorkerHelloLegacyBoundary:
    """``if protocol_version < 1`` warns only for versions strictly below 1.

    Kills boundary mutants ``<= 1`` and ``< 2``: under either, version 1 would
    *wrongly* emit the legacy warning. The info log fires for every supplied
    version; the legacy warning is exclusive to version 0 (and below).
    """

    async def test_version_one_is_not_legacy(self) -> None:
        hub = TermHub()
        st = WorkerTermState()
        await _put(hub, "w1", st)
        with patch("provide.uterm.server.bridge.hub.connection.logger") as mlog:
            applied = await hub.set_worker_hello("w1", "hijack", protocol_version=1)
        assert applied is True
        # The protocol info log fired for version 1 ...
        mlog.info.assert_called_once_with("worker_hello_protocol worker_id=%s version=%d", "w1", 1)
        # ... but the legacy warning must NOT fire (version 1 is not < 1).
        # Mutants `<= 1` / `< 2` would have called warning with the legacy msg.
        legacy_msg = "worker_hello_legacy_protocol worker_id=%s version=%d"
        legacy_calls = [c for c in mlog.warning.call_args_list if c.args and c.args[0] == legacy_msg]
        assert legacy_calls == []

    async def test_version_zero_is_legacy(self) -> None:
        hub = TermHub()
        st = WorkerTermState()
        await _put(hub, "w1", st)
        with patch("provide.uterm.server.bridge.hub.connection.logger") as mlog:
            applied = await hub.set_worker_hello("w1", "hijack", protocol_version=0)
        assert applied is True
        mlog.info.assert_called_once_with("worker_hello_protocol worker_id=%s version=%d", "w1", 0)
        # Version 0 IS legacy under `< 1`: the warning must fire exactly once.
        mlog.warning.assert_called_once_with("worker_hello_legacy_protocol worker_id=%s version=%d", "w1", 0)


# ===========================================================================
# Group 2 — register_browser default `defer_broadcast: bool = False`
# ===========================================================================


class TestRegisterBrowserDeferDefault:
    """The keyword-only default ``defer_broadcast=False`` must NOT defer.

    Kills the mutant that flips the default to ``True``. The hub facade
    (``core_impl.register_browser``) forwards ``defer_broadcast`` *explicitly*,
    so it would mask a ``connection.py`` default flip — we call the service
    method ``hub.connection_mgr.register_browser`` DIRECTLY (no kwarg) so the
    ``connection.py`` default is the one under test.
    """

    async def test_default_does_not_defer(self) -> None:
        hub = TermHub()
        await _put(hub, "w1", WorkerTermState())
        ws = _ws()
        # Call the service directly WITHOUT defer_broadcast -> connection.py
        # default (False) -> NOT deferred. Mutant default True -> deferred.
        await hub.connection_mgr.register_browser("w1", ws, "viewer")
        assert ws not in hub._startup_pending_browsers
        assert hub._startup_pending_browsers == set()

    async def test_explicit_true_does_defer(self) -> None:
        hub = TermHub()
        await _put(hub, "w1", WorkerTermState())
        ws = _ws()
        await hub.connection_mgr.register_browser("w1", ws, "viewer", defer_broadcast=True)
        assert ws in hub._startup_pending_browsers


# ===========================================================================
# Group 3 — _update_lock_state `st.browsers.pop(ws, None)` graceful absent pop
# ===========================================================================


class TestUpdateLockStateBrowsersPopDefault:
    """``st.browsers.pop(ws, None)`` must not raise when ws is absent.

    Kills the mutant ``st.browsers.pop(ws)`` (no default): popping an absent
    key would raise ``KeyError``. The call must return normally and yield the
    all-False tuple for a non-owner, non-owned disconnect.
    """

    def test_pop_absent_ws_does_not_raise(self) -> None:
        hub = TermHub()
        st = WorkerTermState()
        ws = _ws()
        other = _ws()
        # ws is NOT in st.browsers; only an unrelated browser is present.
        st.browsers[other] = "viewer"
        result = hub.connection_mgr._update_lock_state(st, ws, owned_hijack=False)
        assert result == (False, False, False)
        # The other browser is untouched; ws was never present.
        assert st.browsers == {other: "viewer"}

    def test_pop_on_empty_browsers_does_not_raise(self) -> None:
        hub = TermHub()
        st = WorkerTermState()
        ws = _ws()
        # Empty browsers map -> .pop(ws, None) graceful; .pop(ws) -> KeyError.
        result = hub.connection_mgr._update_lock_state(st, ws, owned_hijack=False)
        assert result == (False, False, False)
        assert st.browsers == {}


# ===========================================================================
# Group 4 — _rollback_browser_quota defensive .get/.pop defaults
# ===========================================================================


class TestRollbackBrowserQuotaDefensiveDefaults:
    """Inconsistent-state rollback must not raise.

    ``_ws_principal[ws]`` set to a subject that has NO entry in
    ``_principal_browser_counts``:

    * ``.get(subject_id, 0) - 1`` -> ``-1`` (remaining <= 0) -> ``.pop(subject_id, None)``
      returns None gracefully -> no error, counts stay empty.

    Kills:
    * ``.get(subject_id, None)`` / ``.get(subject_id, )`` -> ``None - 1`` -> ``TypeError``.
    * ``.pop(subject_id)`` (no default) -> ``KeyError``.
    """

    def test_inconsistent_state_rolls_back_without_raising(self) -> None:
        hub = TermHub()
        ws = _ws()
        hub._ws_principal[ws] = "alice"
        # _principal_browser_counts deliberately has NO "alice" entry.
        assert hub._principal_browser_counts == {}
        hub.connection_mgr._rollback_browser_quota(ws)
        # No exception; counts stay empty, ws-principal mapping cleared.
        assert hub._principal_browser_counts == {}
        assert ws not in hub._ws_principal


# ===========================================================================
# Group 5 — _update_lock_state per-principal count defensive .get/.pop defaults
# ===========================================================================


class TestUpdateLockStateCountDefensiveDefaults:
    """Same inconsistent-state defence inside ``_update_lock_state``.

    ``ws`` is a counted browser (in ``_ws_principal``) but its subject has no
    ``_principal_browser_counts`` entry. The decrement path must use the
    defensive defaults and not raise.

    Kills:
    * ``.get(subject_id, None)`` -> ``None - 1`` -> ``TypeError``.
    * ``.pop(subject_id)`` (no default) -> ``KeyError``.
    """

    def test_inconsistent_count_decrement_does_not_raise(self) -> None:
        hub = TermHub()
        st = WorkerTermState()
        ws = _ws()
        st.browsers[ws] = "viewer"
        hub._ws_principal[ws] = "bob"
        # No "bob" entry in the counts map -> .get("bob", 0) defence kicks in.
        assert hub._principal_browser_counts == {}
        result = hub.connection_mgr._update_lock_state(st, ws, owned_hijack=False)
        assert result == (False, False, False)
        assert hub._principal_browser_counts == {}
        assert ws not in hub._ws_principal


# ===========================================================================
# Group 6 — cleanup_browser_disconnect done-callback discards the task
# ===========================================================================


class TestCleanupBrowserDisconnectDoneCallback:
    """``task.add_done_callback(hub._background_tasks.discard)`` removes the task.

    Kills the mutant ``add_done_callback(None)``: once the spawned
    ``on_worker_empty`` task completes, the original callback discards it from
    ``_background_tasks``. With ``None`` the task is never discarded (and the
    completion callback machinery raises ``TypeError`` on a ``None`` callback).
    """

    async def test_completed_callback_task_is_discarded(self) -> None:
        hub = TermHub()
        ran = asyncio.Event()

        async def on_empty(_wid: str) -> None:
            ran.set()

        hub.on_worker_empty = on_empty
        ws = _ws()
        st = WorkerTermState()
        # ws is the ONLY browser -> after pop, browser_count == 0 -> callback fires.
        st.browsers[ws] = "viewer"
        await _put(hub, "w1", st)

        await hub.cleanup_browser_disconnect("w1", ws, False)
        # A task was scheduled and tracked.
        assert len(hub._background_tasks) == 1
        spawned = next(iter(hub._background_tasks))

        # Drive the spawned task to completion, then let the done-callback run.
        await asyncio.wait_for(spawned, 1.0)
        await asyncio.wait_for(ran.wait(), 1.0)
        # Yield so the add_done_callback discard runs on the event loop.
        for _ in range(10):
            if spawned not in hub._background_tasks:
                break
            await asyncio.sleep(0)
        # Original: the completed task is discarded from the tracking set.
        assert spawned not in hub._background_tasks
        assert hub._background_tasks == set()
