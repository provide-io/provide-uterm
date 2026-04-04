#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for src/provide/terminal/hijack/hub/core.py (part 2).

Covers:
- disconnect_worker: was_hijacked logic; broadcast payload correctness;
  notify_hijack_changed / broadcast_hijack_state / prune_if_idle called with
  correct worker_id; logger.debug exercised on close exception.
- set_input_mode: returns exact error string "active_hijack"; broadcast payload
  contains "ts" key.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.bridge.control_channel_helpers import decode_control_payloads
from provide.terminal.bridge.hub import TermHub
from provide.terminal.bridge.models import WorkerTermState


def _make_hub(**kwargs: Any) -> TermHub:
    return TermHub(**kwargs)


def _make_async_ws() -> AsyncMock:
    """Return a mock WebSocket with async send_text and close."""
    ws = AsyncMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# disconnect_worker: was_hijacked logic (kills mutmut_13)
# ---------------------------------------------------------------------------


class TestDisconnectWorkerWasHijackedLogic:
    """was_hijacked must be False when neither hijack_session nor hijack_owner is set.

    Kills mutmut_13:
      was_hijacked = st.hijack_session is not None or st.hijack_owner is None
    With that mutation, a non-hijacked disconnect (hijack_session=None, hijack_owner=None)
    produces was_hijacked=True, wrongly firing notify_hijack_changed and
    broadcast_hijack_state.
    """

    async def test_no_hijack_does_not_call_notify(self) -> None:
        """When no hijack is active, notify_hijack_changed must NOT be called.

        Kills mutmut_13: `st.hijack_owner is None` → True when not hijacked →
        was_hijacked=True → spurious notify.
        """
        notified: list[tuple[str, bool, str | None]] = []

        def _on_changed(worker_id: str, enabled: bool, owner: str | None) -> None:
            notified.append((worker_id, enabled, owner))

        hub = _make_hub(on_hijack_changed=_on_changed)
        worker_ws = _make_async_ws()
        browser_ws = _make_async_ws()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[browser_ws] = "operator"
            # Explicitly no hijack
            st.hijack_owner = None
            st.hijack_session = None

        await hub.disconnect_worker("w1")

        assert notified == [], (
            "notify_hijack_changed must NOT be called when no hijack was active — "
            "mutmut_13 flips `is not None` to `is None` making was_hijacked True always"
        )

    async def test_active_hijack_calls_notify(self) -> None:
        """When a hijack is active, notify_hijack_changed IS called with enabled=False."""
        notified: list[tuple[str, bool, Any]] = []

        def _on_changed(worker_id: str, enabled: bool, owner: str | None) -> None:
            notified.append((worker_id, enabled, owner))

        hub = _make_hub(on_hijack_changed=_on_changed)
        worker_ws = _make_async_ws()
        browser_ws = _make_async_ws()
        hijack_ws = MagicMock()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[browser_ws] = "admin"
            st.hijack_owner = hijack_ws
            st.hijack_owner_expires_at = time.time() + 3600

        await hub.disconnect_worker("w1")

        assert len(notified) == 1
        assert notified[0] == ("w1", False, None)


# ---------------------------------------------------------------------------
# disconnect_worker: broadcast payload (kills mutmut_25, 26, 29-36)
# ---------------------------------------------------------------------------


class TestDisconnectWorkerBroadcastPayload:
    """disconnect_worker must broadcast a 'worker_disconnected' message with the correct
    worker_id to all registered browser sockets.

    Kills:
    - mutmut_25: broadcast(None, ...) — wrong worker_id → browsers of "w1" not reached
    - mutmut_26: broadcast payload dict becomes {} or otherwise mangled
    - mutmut_29-36: individual payload field mutations (type, worker_id key, ts key)
    """

    async def test_broadcast_contains_worker_disconnected_type(self) -> None:
        """Browsers receive a 'worker_disconnected' message type after disconnect."""
        hub = _make_hub()
        worker_ws = _make_async_ws()
        browser_ws = _make_async_ws()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[browser_ws] = "operator"

        await hub.disconnect_worker("w1")

        calls = browser_ws.send_text.call_args_list
        assert calls, "Browser must receive at least one message after worker disconnect"
        payloads = decode_control_payloads([call.args[0] for call in calls])
        types = [p.get("type") for p in payloads]
        assert "worker_disconnected" in types, (
            f"Expected 'worker_disconnected' in broadcast types {types} — "
            "mutmut_29-36 mutate the type string or payload structure"
        )

    async def test_broadcast_contains_correct_worker_id(self) -> None:
        """The 'worker_disconnected' payload must contain the correct worker_id."""
        hub = _make_hub()
        worker_ws = _make_async_ws()
        browser_ws = _make_async_ws()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[browser_ws] = "operator"

        await hub.disconnect_worker("w1")

        calls = browser_ws.send_text.call_args_list
        payloads = decode_control_payloads([call.args[0] for call in calls])
        disconnected = [p for p in payloads if p.get("type") == "worker_disconnected"]
        assert disconnected, "Must have at least one worker_disconnected message"
        assert disconnected[0].get("worker_id") == "w1", (
            f"worker_disconnected payload must contain worker_id='w1', got {disconnected[0]} — "
            "mutmut_25 passes None as worker_id to broadcast"
        )

    async def test_broadcast_contains_ts_key(self) -> None:
        """The 'worker_disconnected' payload must contain a 'ts' timestamp key."""
        hub = _make_hub()
        worker_ws = _make_async_ws()
        browser_ws = _make_async_ws()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[browser_ws] = "operator"

        await hub.disconnect_worker("w1")

        calls = browser_ws.send_text.call_args_list
        payloads = decode_control_payloads([call.args[0] for call in calls])
        disconnected = [p for p in payloads if p.get("type") == "worker_disconnected"]
        assert disconnected
        assert "ts" in disconnected[0], f"worker_disconnected payload must have 'ts' key, got {disconnected[0]}"


# ---------------------------------------------------------------------------
# disconnect_worker: correct worker_id to notify/broadcast/prune (kills mutmut_37,41,43,44)
# ---------------------------------------------------------------------------


class TestDisconnectWorkerCallArgs:
    """notify_hijack_changed, broadcast_hijack_state, and prune_if_idle must all
    receive the correct worker_id (not None or some other value).

    Kills:
    - mutmut_37: notify_hijack_changed(None, ...) — worker_id arg mangled
    - mutmut_41: owner=None kwarg missing from notify_hijack_changed
    - mutmut_43: broadcast_hijack_state(None)
    - mutmut_44: prune_if_idle(None)
    """

    async def test_notify_hijack_changed_receives_correct_worker_id(self) -> None:
        """notify_hijack_changed must be called with worker_id='w1', not None.

        Kills mutmut_37: first arg becomes None.
        """
        hub = _make_hub()
        worker_ws = _make_async_ws()
        browser_ws = _make_async_ws()
        hijack_ws = MagicMock()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[browser_ws] = "admin"
            st.hijack_owner = hijack_ws
            st.hijack_owner_expires_at = time.time() + 3600

        recorded: list[tuple[str, bool, Any]] = []

        def _patched_notify(wid: str, *, enabled: bool, owner: Any = None) -> None:
            recorded.append((wid, enabled, owner))

        hub.notify_hijack_changed = _patched_notify  # type: ignore[method-assign]

        await hub.disconnect_worker("w1")

        assert recorded, "notify_hijack_changed must be called when hijack was active"
        wid, enabled, owner = recorded[0]
        assert wid == "w1", f"notify_hijack_changed must receive worker_id='w1', got {wid!r} — mutmut_37 passes None"
        assert enabled is False
        assert owner is None, (
            f"notify_hijack_changed must receive owner=None, got {owner!r} — mutmut_41 omits the owner kwarg"
        )

    async def test_broadcast_hijack_state_receives_correct_worker_id(self) -> None:
        """broadcast_hijack_state must be called with worker_id='w1', not None.

        Kills mutmut_43: broadcast_hijack_state(None).
        """
        hub = _make_hub()
        worker_ws = _make_async_ws()
        browser_ws = _make_async_ws()
        hijack_ws = MagicMock()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[browser_ws] = "admin"
            st.hijack_owner = hijack_ws
            st.hijack_owner_expires_at = time.time() + 3600

        recorded_ids: list[str | None] = []

        async def _patched_bhs(wid: str) -> None:
            recorded_ids.append(wid)

        hub.broadcast_hijack_state = _patched_bhs  # type: ignore[method-assign]

        await hub.disconnect_worker("w1")

        assert recorded_ids, "broadcast_hijack_state must be called when hijack was active"
        assert recorded_ids[0] == "w1", (
            f"broadcast_hijack_state must receive worker_id='w1', got {recorded_ids[0]!r} — mutmut_43 passes None"
        )

    async def test_prune_if_idle_receives_correct_worker_id(self) -> None:
        """prune_if_idle must be called with worker_id='w1', not None.

        Kills mutmut_44: prune_if_idle(None).
        """
        hub = _make_hub()
        worker_ws = _make_async_ws()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            # No browsers — prune will remove the state

        recorded_ids: list[str | None] = []

        async def _patched_prune(wid: str) -> None:
            recorded_ids.append(wid)

        hub.prune_if_idle = _patched_prune  # type: ignore[method-assign]

        await hub.disconnect_worker("w1")

        assert recorded_ids, "prune_if_idle must always be called at the end of disconnect_worker"
        assert recorded_ids[0] == "w1", (
            f"prune_if_idle must receive worker_id='w1', got {recorded_ids[0]!r} — mutmut_44 passes None"
        )


# ---------------------------------------------------------------------------
# disconnect_worker: logger.debug on close exception (kills mutmut_17-24)
# ---------------------------------------------------------------------------


class TestDisconnectWorkerCloseException:
    """When ws.close() raises, logger.debug must log the worker_id and exception.

    Kills mutmut_17-24: mutations to the logger.debug format string or arguments
    that change 'worker_id=%s' to not contain the real worker_id, or corrupt 'exc'.
    """

    async def test_close_exception_logged_with_worker_id(self, caplog: pytest.LogCaptureFixture) -> None:
        """close() exception is caught and logged at DEBUG level with the correct worker_id."""
        import logging

        hub = _make_hub()
        browser_ws = _make_async_ws()

        worker_ws = AsyncMock()
        worker_ws.close = AsyncMock(side_effect=RuntimeError("close failed"))

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[browser_ws] = "operator"

        with caplog.at_level(logging.DEBUG, logger="provide.terminal.bridge.hub.core"):
            await hub.disconnect_worker("w1")

        close_logs = [r for r in caplog.records if "disconnect_worker" in r.message]
        assert close_logs, "Expected at least one debug log for disconnect_worker close error"
        assert "w1" in close_logs[0].message, (
            f"Log message must contain worker_id 'w1', got: {close_logs[0].message!r} — "
            "mutmut_17-24 corrupt the worker_id argument in logger.debug"
        )
        assert "close failed" in close_logs[0].message, (
            f"Log message must contain the exception text, got: {close_logs[0].message!r}"
        )


# ---------------------------------------------------------------------------
# set_input_mode: exact error string (kills mutmut_13, 14)
# ---------------------------------------------------------------------------


class TestSetInputModeActiveHijackError:
    """set_input_mode must return the exact string 'active_hijack' when rejected.

    Kills:
    - mutmut_13: "active_hijack" → "XXactive_hijackXX" or similar mangling
    - mutmut_14: "active_hijack" → "ACTIVE_HIJACK" or case-change
    """

    async def test_returns_exact_active_hijack_string(self) -> None:
        """set_input_mode returns (False, 'active_hijack') when hijack is active."""
        hub = _make_hub()
        hijack_ws = MagicMock()
        worker_ws = MagicMock()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.hijack_owner = hijack_ws
            st.hijack_owner_expires_at = time.time() + 3600

        ok, err = await hub.set_input_mode("w1", "open")

        assert ok is False
        assert err == "active_hijack", (
            f"Expected error string 'active_hijack', got {err!r} — "
            "mutmut_13/14 mangle the error string (case or garbling)"
        )

    async def test_not_found_returns_not_found_string(self) -> None:
        """set_input_mode returns (False, 'not_found') for unknown workers."""
        hub = _make_hub()
        ok, err = await hub.set_input_mode("no-such", "open")
        assert ok is False
        assert err == "not_found"

    async def test_no_hijack_allows_open_mode(self) -> None:
        """set_input_mode succeeds with 'open' when no hijack is active."""
        hub = _make_hub()

        async with hub._lock:
            hub._workers.setdefault("w1", WorkerTermState())

        ok, err = await hub.set_input_mode("w1", "open")
        assert ok is True
        assert err is None


# ---------------------------------------------------------------------------
# set_input_mode: broadcast payload contains 'ts' (kills mutmut_26, 27)
# ---------------------------------------------------------------------------


class TestSetInputModeBroadcastPayload:
    """set_input_mode broadcasts an 'input_mode_changed' message with a 'ts' timestamp.

    Kills:
    - mutmut_26: "ts" key changed to "XXtsXX" or similar
    - mutmut_27: time.time() → 0 or other mutation of the timestamp value
    """

    async def test_broadcast_contains_ts_key(self) -> None:
        """The input_mode_changed broadcast payload must contain a 'ts' key."""
        hub = _make_hub()
        browser_ws = _make_async_ws()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.browsers[browser_ws] = "operator"

        before = time.time()
        await hub.set_input_mode("w1", "open")
        after = time.time()

        calls = browser_ws.send_text.call_args_list
        assert calls, "Browser must receive at least one message after set_input_mode"
        payloads = decode_control_payloads([call.args[0] for call in calls])
        mode_msgs = [p for p in payloads if p.get("type") == "input_mode_changed"]
        assert mode_msgs, f"Expected 'input_mode_changed' in broadcast, got: {payloads}"

        msg = mode_msgs[0]
        assert "ts" in msg, (
            f"input_mode_changed payload must contain 'ts' key, got {msg!r} — mutmut_26 renames 'ts' to something else"
        )
        assert isinstance(msg["ts"], (int, float)), (
            f"'ts' must be a numeric timestamp, got {msg['ts']!r} — "
            "mutmut_27 replaces time.time() with a non-numeric value"
        )
        assert before <= msg["ts"] <= after + 1, f"'ts' must be a recent timestamp, got {msg['ts']}"

    async def test_broadcast_contains_input_mode_field(self) -> None:
        """The input_mode_changed payload must contain the new mode value."""
        hub = _make_hub()
        browser_ws = _make_async_ws()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.browsers[browser_ws] = "operator"

        await hub.set_input_mode("w1", "open")

        calls = browser_ws.send_text.call_args_list
        payloads = decode_control_payloads([call.args[0] for call in calls])
        mode_msgs = [p for p in payloads if p.get("type") == "input_mode_changed"]
        assert mode_msgs
        assert mode_msgs[0].get("input_mode") == "open"
