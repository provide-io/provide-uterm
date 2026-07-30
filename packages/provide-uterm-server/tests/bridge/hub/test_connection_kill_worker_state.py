#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing unit tests for the worker-state setters on ConnectionManager.

Targets three methods on
:class:`provide.uterm.server.bridge.hub.connection.ConnectionManager`:

* :meth:`set_worker_hello` — pins the unknown-worker ``False`` return, the
  hijack-blocked ``open`` mode ``False`` return (with the warning log and the
  ``input_mode`` left UNCHANGED), the success path (``input_mode`` applied,
  ``protocol_version`` recorded only when supplied), and the two protocol logs
  (info always when a version is given, plus the legacy warning when version<1)
  — including that the protocol logs fire even for an unknown worker because
  they run BEFORE the registry lookup.
* :meth:`set_worker_tunnel_flag` — pins ``st.is_tunnel_worker`` set to the
  exact value and the unknown-worker noop.
* :meth:`update_last_snapshot` — pins ``st.last_snapshot`` set to the exact
  object and the unknown-worker noop.

Every test constructs a FRESH :class:`TermHub` (as in
``test_connections_coverage.py``), seeds worker state via
``async with hub._lock: hub.registry._workers[wid] = WorkerTermState()`` and drives the
methods through the hub facade. Observability is verified by patching the
``logger`` in the connection module and asserting the exact event string +
positional args.
"""

from __future__ import annotations

import time
from unittest.mock import patch

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

_LOGGER_PATH = "provide.uterm.server.bridge.hub.connection.logger"


async def _seed_worker(hub: TermHub, worker_id: str, st: WorkerTermState) -> None:
    """Install *st* as the worker state for *worker_id* under the hub lock."""
    async with hub._lock:
        hub.registry._workers[worker_id] = st


def _active_rest_session() -> HijackSession:
    """A REST hijack session whose lease is comfortably in the future."""
    now = time.monotonic()
    return HijackSession(
        hijack_id="hid-active",
        owner="alice",
        acquired_at=now,
        lease_expires_at=now + 600,
        last_heartbeat=now,
    )


# ---------------------------------------------------------------------------
# set_worker_hello
# ---------------------------------------------------------------------------


class TestSetWorkerHello:
    async def test_unknown_worker_returns_false_no_protocol_logs(self) -> None:
        """Unknown worker_id -> False, no state, and (no version) no protocol logs."""
        hub = TermHub()
        with patch(_LOGGER_PATH) as logger:
            result = await hub.set_worker_hello("ghost", "open")
        assert result is False
        # Nothing was created for the ghost worker.
        assert hub.registry._workers.get("ghost") is None
        # No protocol_version => neither protocol log fired.
        logger.info.assert_not_called()
        logger.warning.assert_not_called()

    async def test_unknown_worker_still_logs_protocol_info_before_lookup(self) -> None:
        """Protocol info log fires BEFORE the registry lookup, even for a ghost worker."""
        hub = TermHub()
        with patch(_LOGGER_PATH) as logger:
            result = await hub.set_worker_hello("ghost", "hijack", protocol_version=3)
        assert result is False
        logger.info.assert_called_once_with("worker_hello_protocol worker_id=%s version=%d", "ghost", 3)
        # version 3 >= 1 -> no legacy warning.
        logger.warning.assert_not_called()

    async def test_unknown_worker_legacy_protocol_emits_both_logs(self) -> None:
        """version<1 for a ghost worker: info log + legacy warning both fire, returns False."""
        hub = TermHub()
        with patch(_LOGGER_PATH) as logger:
            result = await hub.set_worker_hello("ghost", "hijack", protocol_version=0)
        assert result is False
        logger.info.assert_called_once_with("worker_hello_protocol worker_id=%s version=%d", "ghost", 0)
        logger.warning.assert_called_once_with("worker_hello_legacy_protocol worker_id=%s version=%d", "ghost", 0)

    async def test_open_mode_blocked_while_hijacked_returns_false_mode_unchanged(self) -> None:
        """mode=='open' while a hijack is active -> False, warning log, input_mode UNCHANGED."""
        hub = TermHub()
        worker_id = "w-blocked"
        st = WorkerTermState()
        st.input_mode = "hijack"
        st.hijack_session = _active_rest_session()
        await _seed_worker(hub, worker_id, st)

        with patch(_LOGGER_PATH) as logger:
            result = await hub.set_worker_hello(worker_id, "open")

        assert result is False
        # Mode was NOT changed to "open".
        assert hub.registry._workers[worker_id].input_mode == "hijack"
        logger.warning.assert_called_once_with(
            "worker_hello_mode_blocked worker_id=%s — a hello may not lower a decided mode to open",
            worker_id,
        )
        # No protocol_version supplied -> the protocol info log never fired.
        logger.info.assert_not_called()

    async def test_open_mode_allowed_when_not_hijacked_sets_mode_returns_true(self) -> None:
        """mode=='open' with no active hijack -> True, input_mode becomes 'open'."""
        hub = TermHub()
        worker_id = "w-open-ok"
        st = WorkerTermState()
        st.input_mode = "hijack"
        await _seed_worker(hub, worker_id, st)

        with patch(_LOGGER_PATH) as logger:
            result = await hub.set_worker_hello(worker_id, "open")

        assert result is True
        assert hub.registry._workers[worker_id].input_mode == "open"
        # No version supplied -> protocol_version stays None, no protocol logs.
        assert hub.registry._workers[worker_id].protocol_version is None
        logger.info.assert_not_called()
        logger.warning.assert_not_called()

    async def test_hijack_mode_allowed_while_hijacked_sets_mode_returns_true(self) -> None:
        """mode=='hijack' is NOT blocked even while hijacked -> True, mode set to 'hijack'."""
        hub = TermHub()
        worker_id = "w-hijack-mode"
        st = WorkerTermState()
        st.input_mode = "open"
        st.hijack_session = _active_rest_session()
        await _seed_worker(hub, worker_id, st)

        with patch(_LOGGER_PATH) as logger:
            result = await hub.set_worker_hello(worker_id, "hijack")

        assert result is True
        assert hub.registry._workers[worker_id].input_mode == "hijack"
        # The block-warning is only for the open path; it must NOT fire here.
        logger.warning.assert_not_called()

    async def test_success_with_protocol_version_records_version_and_logs_info(self) -> None:
        """Known worker + version>=1 -> True, input_mode set, protocol_version recorded, info log only."""
        hub = TermHub()
        worker_id = "w-proto-ok"
        st = WorkerTermState()
        st.input_mode = "hijack"
        st.protocol_version = None
        await _seed_worker(hub, worker_id, st)

        with patch(_LOGGER_PATH) as logger:
            result = await hub.set_worker_hello(worker_id, "open", protocol_version=2)

        assert result is True
        assert hub.registry._workers[worker_id].input_mode == "open"
        assert hub.registry._workers[worker_id].protocol_version == 2
        logger.info.assert_called_once_with("worker_hello_protocol worker_id=%s version=%d", worker_id, 2)
        # version 2 >= 1 -> no legacy warning.
        logger.warning.assert_not_called()

    async def test_success_with_legacy_version_records_version_and_warns(self) -> None:
        """Known worker + version<1 -> True, protocol_version recorded, info log + legacy warning."""
        hub = TermHub()
        worker_id = "w-proto-legacy"
        st = WorkerTermState()
        st.input_mode = "hijack"
        await _seed_worker(hub, worker_id, st)

        with patch(_LOGGER_PATH) as logger:
            result = await hub.set_worker_hello(worker_id, "open", protocol_version=0)

        assert result is True
        assert hub.registry._workers[worker_id].input_mode == "open"
        assert hub.registry._workers[worker_id].protocol_version == 0
        logger.info.assert_called_once_with("worker_hello_protocol worker_id=%s version=%d", worker_id, 0)
        logger.warning.assert_called_once_with("worker_hello_legacy_protocol worker_id=%s version=%d", worker_id, 0)

    async def test_success_without_protocol_version_leaves_version_untouched(self) -> None:
        """No version supplied -> protocol_version is NOT overwritten (stays at prior value)."""
        hub = TermHub()
        worker_id = "w-proto-keep"
        st = WorkerTermState()
        st.input_mode = "hijack"
        st.protocol_version = 7
        await _seed_worker(hub, worker_id, st)

        with patch(_LOGGER_PATH) as logger:
            result = await hub.set_worker_hello(worker_id, "open")

        assert result is True
        assert hub.registry._workers[worker_id].input_mode == "open"
        # Unchanged: the `if protocol_version is not None` guard kept the old value.
        assert hub.registry._workers[worker_id].protocol_version == 7
        logger.info.assert_not_called()
        logger.warning.assert_not_called()


# ---------------------------------------------------------------------------
# set_worker_tunnel_flag
# ---------------------------------------------------------------------------


class TestSetWorkerTunnelFlag:
    async def test_sets_flag_true(self) -> None:
        """Known worker -> is_tunnel_worker set to True exactly."""
        hub = TermHub()
        worker_id = "w-tunnel-on"
        st = WorkerTermState()
        st.is_tunnel_worker = False
        await _seed_worker(hub, worker_id, st)

        result = await hub.set_worker_tunnel_flag(worker_id, True)

        assert result is None
        assert hub.registry._workers[worker_id].is_tunnel_worker is True

    async def test_sets_flag_false(self) -> None:
        """Known worker -> is_tunnel_worker set to False exactly (distinct from the True arc)."""
        hub = TermHub()
        worker_id = "w-tunnel-off"
        st = WorkerTermState()
        st.is_tunnel_worker = True
        await _seed_worker(hub, worker_id, st)

        result = await hub.set_worker_tunnel_flag(worker_id, False)

        assert result is None
        assert hub.registry._workers[worker_id].is_tunnel_worker is False

    async def test_noop_for_unknown_worker(self) -> None:
        """Unknown worker_id -> noop, no WorkerTermState created."""
        hub = TermHub()
        result = await hub.set_worker_tunnel_flag("ghost", True)
        assert result is None
        assert hub.registry._workers.get("ghost") is None

    async def test_does_not_touch_other_workers(self) -> None:
        """Setting the flag on one worker leaves a sibling worker's flag unchanged."""
        hub = TermHub()
        target = WorkerTermState()
        target.is_tunnel_worker = False
        other = WorkerTermState()
        other.is_tunnel_worker = False
        await _seed_worker(hub, "w-target", target)
        await _seed_worker(hub, "w-other", other)

        await hub.set_worker_tunnel_flag("w-target", True)

        assert hub.registry._workers["w-target"].is_tunnel_worker is True
        assert hub.registry._workers["w-other"].is_tunnel_worker is False


# ---------------------------------------------------------------------------
# update_last_snapshot
# ---------------------------------------------------------------------------


class TestUpdateLastSnapshot:
    async def test_stores_snapshot_object_identity(self) -> None:
        """Known worker -> last_snapshot is set to the exact dict passed in."""
        hub = TermHub()
        worker_id = "w-snap"
        st = WorkerTermState()
        st.last_snapshot = None
        await _seed_worker(hub, worker_id, st)

        snapshot = {"screen": "hello", "rows": 24}
        result = await hub.update_last_snapshot(worker_id, snapshot)

        assert result is None
        assert hub.registry._workers[worker_id].last_snapshot is snapshot

    async def test_overwrites_previous_snapshot(self) -> None:
        """A second update replaces the stored snapshot (not merged/appended)."""
        hub = TermHub()
        worker_id = "w-snap-2"
        st = WorkerTermState()
        st.last_snapshot = {"old": True}
        await _seed_worker(hub, worker_id, st)

        new_snapshot = {"new": True}
        await hub.update_last_snapshot(worker_id, new_snapshot)

        assert hub.registry._workers[worker_id].last_snapshot is new_snapshot

    async def test_noop_for_unknown_worker(self) -> None:
        """Unknown worker_id -> noop, no WorkerTermState created."""
        hub = TermHub()
        result = await hub.update_last_snapshot("ghost", {"a": 1})
        assert result is None
        assert hub.registry._workers.get("ghost") is None

    async def test_does_not_touch_other_workers(self) -> None:
        """Updating one worker's snapshot leaves a sibling worker's snapshot unchanged."""
        hub = TermHub()
        target = WorkerTermState()
        sentinel = {"sentinel": True}
        other = WorkerTermState()
        other.last_snapshot = sentinel
        await _seed_worker(hub, "w-target", target)
        await _seed_worker(hub, "w-other", other)

        await hub.update_last_snapshot("w-target", {"fresh": 1})

        assert hub.registry._workers["w-target"].last_snapshot == {"fresh": 1}
        assert hub.registry._workers["w-other"].last_snapshot is sentinel


__all__ = [
    "TestSetWorkerHello",
    "TestSetWorkerTunnelFlag",
    "TestUpdateLastSnapshot",
]
