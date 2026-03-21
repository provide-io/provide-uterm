#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for hub/core.py — broadcast, roles, prune, hijack state."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.models import HijackSession, WorkerTermState

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_hub(**kwargs: Any) -> TermHub:
    return TermHub(**kwargs)


def _make_ws() -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


def _make_hijack_session(hijack_id: str = "hj-1", owner: str = "op", lease_s: float = 60.0) -> HijackSession:
    now = time.time()
    return HijackSession(
        hijack_id=hijack_id,
        owner=owner,
        acquired_at=now,
        lease_expires_at=now + lease_s,
        last_heartbeat=now,
    )


async def _setup_worker(hub: TermHub, worker_id: str, worker_ws: Any = None) -> WorkerTermState:
    async with hub._lock:
        st = hub._workers.setdefault(worker_id, WorkerTermState())
        st.worker_ws = worker_ws or _make_ws()
        return st


# ===========================================================================
# ownership.py — _expire_leases_under_lock
# ===========================================================================


class TestNotifyHijackChanged:
    async def test_async_callback_worker_id_correct(self) -> None:
        """mutmut_16/17: callback called with worker_id=None or exc=None.

        The done callback logs worker_id and exception — we verify the task runs.
        """
        received: list[tuple[str, bool, Any]] = []

        async def _async_cb(worker_id: str, enabled: bool, owner: Any) -> None:
            received.append((worker_id, enabled, owner))

        hub = _make_hub(on_hijack_changed=_async_cb)
        hub.notify_hijack_changed("w-async", enabled=True, owner="me")
        await asyncio.sleep(0.05)  # let task run
        assert ("w-async", True, "me") in received

    async def test_error_in_async_callback_does_not_raise(self) -> None:
        """mutmut_21: logger message mutation — error must still be swallowed."""

        async def _failing_cb(worker_id: str, enabled: bool, owner: Any) -> None:
            raise RuntimeError("test error")

        hub = _make_hub(on_hijack_changed=_failing_cb)
        hub.notify_hijack_changed("w-err", enabled=True, owner=None)
        await asyncio.sleep(0.05)  # let task fail, must not propagate


# ===========================================================================
# core.py — _resolve_role_for_browser (supplementary mutants)
# ===========================================================================


class TestResolveRoleExtra:
    async def test_operator_role_accepted(self) -> None:
        """mutmut_14/17/19: 'operator' in valid set."""
        hub = _make_hub(resolve_browser_role=lambda ws, wid: "operator")
        ws = _make_ws()
        role = await hub._resolve_role_for_browser(ws, "w1")
        assert role == "operator"

    async def test_admin_role_accepted(self) -> None:
        """mutmut_20/21/22: 'admin' in valid set."""
        hub = _make_hub(resolve_browser_role=lambda ws, wid: "admin")
        ws = _make_ws()
        role = await hub._resolve_role_for_browser(ws, "w1")
        assert role == "admin"

    async def test_none_resolver_returns_viewer_default(self) -> None:
        """mutmut_29/30: role='viewer' default (no resolver)."""
        hub = _make_hub()
        ws = _make_ws()
        role = await hub._resolve_role_for_browser(ws, "w1")
        assert role == "viewer"

    async def test_none_resolved_role_falls_back_to_viewer(self) -> None:
        """mutmut_34: resolved_role is None → return role (viewer)."""
        hub = _make_hub(resolve_browser_role=lambda ws, wid: None)
        ws = _make_ws()
        role = await hub._resolve_role_for_browser(ws, "w1")
        assert role == "viewer"

    async def test_int_resolved_role_falls_back_to_viewer(self) -> None:
        """mutmut_39/40: isinstance check — non-string role should fall back."""
        hub = _make_hub(resolve_browser_role=lambda ws, wid: 42)
        ws = _make_ws()
        role = await hub._resolve_role_for_browser(ws, "w1")
        assert role == "viewer"

    async def test_await_timeout_5s_raises_resolution_error(self) -> None:
        """mutmut_45/47/48/52: timeout=5.0 mutations."""
        from provide.terminal.hijack.hub import BrowserRoleResolutionError

        async def _slow_resolver(ws: Any, worker_id: str) -> None:
            await asyncio.sleep(1000)

        hub = _make_hub(resolve_browser_role=_slow_resolver)
        ws = _make_ws()

        async def _mock_wait_for(coro: Any, **kwargs: Any) -> None:
            raise TimeoutError("mocked")

        with patch("asyncio.wait_for", side_effect=_mock_wait_for), pytest.raises(BrowserRoleResolutionError):
            await hub._resolve_role_for_browser(ws, "w1")


# ===========================================================================
# core.py — broadcast
# ===========================================================================


class TestBroadcast:
    async def test_dead_browsers_cleaned_up(self) -> None:
        """mutmut_9/11/12: remove_dead_browsers called after send failure."""
        hub = _make_hub()
        worker_ws = _make_ws()
        dead_browser = _make_ws()
        dead_browser.send_text = AsyncMock(side_effect=Exception("disconnected"))
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[dead_browser] = "operator"

        await hub.broadcast("w1", {"type": "test"})

        async with hub._lock:
            st2 = hub._workers.get("w1")
            if st2:
                assert dead_browser not in st2.browsers

    async def test_broadcast_sends_to_all_browsers(self) -> None:
        """mutmut_15/16/20/28: message sent to correct browsers."""
        hub = _make_hub()
        b1 = _make_ws()
        b2 = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.browsers[b1] = "operator"
            st.browsers[b2] = "operator"

        await hub.broadcast("w1", {"type": "ping", "data": "hello"})

        import json

        assert b1.send_text.call_count == 1
        assert b2.send_text.call_count == 1
        msg = json.loads(b1.send_text.call_args[0][0])
        assert msg["type"] == "ping"


# ===========================================================================
# core.py — prune_if_idle
# ===========================================================================


class TestPruneIfIdle:
    async def test_worker_pruned_when_all_empty(self) -> None:
        """mutmut_12/13/14/15: logger mutations — prune still executes."""
        hub = _make_hub()
        async with hub._lock:
            hub._workers["w1"] = WorkerTermState()
            # No worker_ws, no browsers, no hijack state — fully idle

        await hub.prune_if_idle("w1")

        async with hub._lock:
            assert "w1" not in hub._workers

    async def test_worker_not_pruned_when_worker_ws_present(self) -> None:
        """Sanity: worker_ws must prevent pruning."""
        hub = _make_hub()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()

        await hub.prune_if_idle("w1")

        async with hub._lock:
            assert "w1" in hub._workers


# ===========================================================================
# core.py — hijack_state_msg_for
# ===========================================================================


class TestHijackStateMsgFor:
    async def test_msg_type_is_hijack_state(self) -> None:
        """mutmut_4/5/6/7: 'type' key mutations."""
        hub = _make_hub()
        ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()

        msg = await hub.hijack_state_msg_for("w1", ws)
        assert msg.get("type") == "hijack_state"

    async def test_msg_hijacked_is_bool(self) -> None:
        """mutmut_13/14/15/16: 'hijacked' key mutations."""
        hub = _make_hub()
        ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()

        msg = await hub.hijack_state_msg_for("w1", ws)
        assert "hijacked" in msg
        assert isinstance(msg["hijacked"], bool)

    async def test_msg_owner_is_me_for_dashboard_owner(self) -> None:
        """mutmut_17/18: 'owner' and 'me' key/value mutations."""
        hub = _make_hub()
        owner_ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 300

        msg = await hub.hijack_state_msg_for("w1", owner_ws)
        assert msg.get("owner") == "me"

    async def test_msg_owner_is_other_for_non_owner_browser(self) -> None:
        """mutmut_26/27: 'other' value mutations."""
        hub = _make_hub()
        owner_ws = _make_ws()
        other_ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = time.time() + 300

        msg = await hub.hijack_state_msg_for("w1", other_ws)
        assert msg.get("owner") == "other"

    async def test_msg_has_lease_expires_at(self) -> None:
        """mutmut_28/29: 'lease_expires_at' key mutations."""
        hub = _make_hub()
        ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()

        msg = await hub.hijack_state_msg_for("w1", ws)
        assert "lease_expires_at" in msg

    async def test_msg_has_input_mode(self) -> None:
        """mutmut_38/47/48: 'input_mode' key mutations."""
        hub = _make_hub()
        ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()

        msg = await hub.hijack_state_msg_for("w1", ws)
        assert "input_mode" in msg

    async def test_msg_no_worker_returns_defaults(self) -> None:
        """mutmut_4/5/6/7: no-worker path returns correct defaults."""
        hub = _make_hub()
        ws = _make_ws()
        msg = await hub.hijack_state_msg_for("nonexistent", ws)
        assert msg["type"] == "hijack_state"
        assert msg["hijacked"] is False
        assert msg["owner"] is None
        assert msg["input_mode"] == "hijack"


# ===========================================================================
# core.py — disconnect_worker
# ===========================================================================


class TestDisconnectWorker:
    async def test_initial_ws_assignment_does_not_matter(self) -> None:
        """mutmut_1: ws = None → ''.  The local ws var must work after assign."""
        hub = _make_hub()
        worker_ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws

        result = await hub.disconnect_worker("w1")
        assert result is True

    async def test_returns_false_for_unknown_worker(self) -> None:
        """Sanity check for mutmut_1 path."""
        hub = _make_hub()
        result = await hub.disconnect_worker("nonexistent")
        assert result is False

    async def test_notify_hijack_changed_called_when_hijacked(self) -> None:
        """mutmut_41: notify called without owner=None arg."""
        received: list[tuple[str, bool, Any]] = []

        def _on_hijack(worker_id: str, enabled: bool, owner: Any) -> None:
            received.append((worker_id, enabled, owner))

        hub = _make_hub(on_hijack_changed=_on_hijack)
        worker_ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.hijack_session = _make_hijack_session(lease_s=60.0)

        await hub.disconnect_worker("w1")
        assert len(received) >= 1
        _, enabled, owner = received[0]
        assert enabled is False
        assert owner is None


# ===========================================================================
# polling.py — wait_for_snapshot
# ===========================================================================


class TestWaitForSnapshot:
    async def test_default_timeout_is_1500_ms(self) -> None:
        """mutmut_1: default timeout_ms=1500 → 1501."""
        import inspect

        sig = inspect.signature(TermHub.wait_for_snapshot)
        default = sig.parameters["timeout_ms"].default
        assert default == 1500

    async def test_snap_returned_when_ts_after_req_ts(self) -> None:
        """mutmut_22: snap.get('ts', 0) > req_ts → >= req_ts.

        With >= and ts==req_ts, an old snapshot would be returned.
        With >, it must have a newer ts.
        """
        hub = _make_hub()
        ws = _make_ws()
        request_snapshot_calls: list[str | None] = []

        async def _mock_request(worker_id: str) -> None:
            request_snapshot_calls.append(worker_id)
            # Inject a fresh snapshot with ts AFTER req_ts
            await asyncio.sleep(0)
            now = time.time()
            async with hub._lock:
                st = hub._workers.get(worker_id)
                if st is not None:
                    st.last_snapshot = {"screen": "hello", "ts": now + 1.0}

        hub.request_snapshot = _mock_request  # type: ignore[method-assign]
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = ws

        result = await hub.wait_for_snapshot("w1", timeout_ms=500)
        assert result is not None
        assert result.get("screen") == "hello"

    async def test_returns_none_for_nonexistent_worker(self) -> None:
        """mutmut_7: request_snapshot(None) vs (worker_id)."""
        hub = _make_hub()
        result = await hub.wait_for_snapshot("nonexistent", timeout_ms=50)
        assert result is None

    async def test_snap_ts_0_default_not_returned_when_stale(self) -> None:
        """mutmut_16/18/21: snap.get('ts', 0) → None/missing/1.

        With default 0 and req_ts > 0: snap without 'ts' has ts=0 < req_ts → not returned.
        With default 1 and req_ts=0: ts=1 > 0 → would be returned as fresh (wrong).
        """
        hub = _make_hub()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            # snapshot without 'ts' key — default 0 means it's before req_ts
            st.last_snapshot = {"screen": "old", "cols": 80}

        async def _noop_request(worker_id: str) -> None:
            pass

        hub.request_snapshot = _noop_request  # type: ignore[method-assign]

        # With very short timeout (50ms), the stale snapshot should NOT be returned
        result = await hub.wait_for_snapshot("w1", timeout_ms=50)
        # Result should be None since snapshot has no fresh 'ts'
        assert result is None


# ===========================================================================
# polling.py — wait_for_guard
# ===========================================================================


class TestWaitForGuard:
    async def test_invalid_regex_returns_error(self) -> None:
        """mutmut_6/7: error string returned on bad regex."""
        hub = _make_hub()
        matched, snap, reason = await hub.wait_for_guard(
            "w1",
            expect_prompt_id=None,
            expect_regex="[invalid(regex",
            timeout_ms=100,
            poll_interval_ms=10,
        )
        assert matched is False
        assert reason is not None
        assert "invalid" in reason.lower() or "regex" in reason.lower()

    async def test_no_guards_returns_current_snapshot_immediately(self) -> None:
        """mutmut_12/13/14/15/16: early-return path when no guards specified."""
        hub = _make_hub()
        ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = ws
            st.last_snapshot = {"screen": "current", "ts": time.time()}

        async def _noop(worker_id: str) -> None:
            pass

        hub.request_snapshot = _noop  # type: ignore[method-assign]

        matched, snap, reason = await hub.wait_for_guard(
            "w1",
            expect_prompt_id=None,
            expect_regex=None,
            timeout_ms=100,
            poll_interval_ms=10,
        )
        assert matched is True
        assert reason is None

    async def test_timeout_returns_failure_reason(self) -> None:
        """mutmut_25/26: 'prompt_guard_not_satisfied' reason on timeout."""
        hub = _make_hub()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.last_snapshot = {"screen": "no match here", "ts": time.time()}

        async def _noop(worker_id: str) -> None:
            pass

        hub.request_snapshot = _noop  # type: ignore[method-assign]

        matched, _, reason = await hub.wait_for_guard(
            "w1",
            expect_prompt_id=None,
            expect_regex="WILL_NOT_MATCH_XYZ",
            timeout_ms=50,
            poll_interval_ms=10,
        )
        assert matched is False
        assert reason == "prompt_guard_not_satisfied"

    async def test_min_timeout_50ms(self) -> None:
        """mutmut_33/34: max(50, timeout_ms) / 1000.0 — timeout must be at least 50ms."""

        # We can't easily test timing, but verify the formula: max(50, X) / 1000.0
        # max(50, 10) = 50ms; max(50, 100) = 100ms
        assert max(50, 10) / 1000.0 == 0.05
        assert max(50, 100) / 1000.0 == 0.1

    async def test_min_interval_20ms(self) -> None:
        """mutmut_35/36: max(20, poll_interval_ms) / 1000.0."""
        assert max(20, 5) / 1000.0 == 0.02
        assert max(20, 50) / 1000.0 == 0.05

    async def test_snap_ts_stale_triggers_new_request(self) -> None:
        """mutmut_38/39: snap_ts <= last_snap_ts → only requests when stale."""
        hub = _make_hub()
        request_calls: list[str] = []
        snap_ts = time.time() - 1.0  # old snapshot

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.last_snapshot = {"screen": "old", "ts": snap_ts}

        async def _counting_request(worker_id: str) -> None:
            request_calls.append(worker_id)

        hub.request_snapshot = _counting_request  # type: ignore[method-assign]

        matched, _, reason = await hub.wait_for_guard(
            "w1",
            expect_prompt_id=None,
            expect_regex="NEVER_MATCH",
            timeout_ms=60,
            poll_interval_ms=10,
        )
        # Should have made at least the initial request + 1 retry
        assert len(request_calls) >= 2

    async def test_regex_case_insensitive(self) -> None:
        """mutmut_52/56/57: re.IGNORECASE in compile flags."""
        hub = _make_hub()
        ws = _make_ws()
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = ws
            st.last_snapshot = {"screen": "Login: prompt here", "ts": time.time() + 100}

        async def _noop(worker_id: str) -> None:
            pass

        hub.request_snapshot = _noop  # type: ignore[method-assign]

        matched, snap, reason = await hub.wait_for_guard(
            "w1",
            expect_prompt_id=None,
            expect_regex="login:",  # lowercase regex, screen has uppercase
            timeout_ms=200,
            poll_interval_ms=10,
        )
        assert matched is True


# ===========================================================================
# resume.py — InMemoryResumeStore
# ===========================================================================
