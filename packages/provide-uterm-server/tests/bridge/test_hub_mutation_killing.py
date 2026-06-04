#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for hub connections.py and core.py.

Targets surviving mutants in:
- force_release_hijack (control message fields, worker-not-found, expires_at)
- can_send_input (expired-hijack owner cannot send: 'and' vs 'or')
- request_analysis (message dict keys)
- allow_rest_send_for (per-client separate buckets)
- _resolve_role_for_browser (metric value, error worker_id, "viewer" valid role, invalid fallback)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.server.bridge.hub import BrowserRoleResolutionError, TermHub
from provide.uterm.server.bridge.models import WorkerTermState
from tests.bridge.control_channel_helpers import decode_control_payload


def _make_hub(**kwargs: Any) -> TermHub:
    return TermHub(**kwargs)


def _make_ws() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# force_release_hijack — control message fields + edge cases
# ---------------------------------------------------------------------------


class TestForceReleaseHijackControlMsg:
    async def test_returns_false_when_worker_not_registered(self) -> None:
        """Worker not found → return False (not True).

        Kills _mutmut_9: return False → return True.
        """
        hub = _make_hub()
        result = await hub.force_release_hijack("nonexistent-worker")
        assert result is False, f"Expected False for unknown worker, got {result!r}"

    async def test_control_msg_sent_with_correct_fields(self) -> None:
        """Worker receives control/resume message with correct field names and values.

        Kills:
          _mutmut_1:  owner='server-forced' → None
          _mutmut_26: type='control' key mutated
          _mutmut_31: action='resume' key mutated
          _mutmut_32: action='resume' value mutated
          _mutmut_35: owner= key mutated
          _mutmut_38: lease_s=0 → 1
        """
        hub = _make_hub()
        worker_ws = AsyncMock()
        sent_msgs: list[dict[str, Any]] = []

        async def _capture(msg: dict[str, Any]) -> None:
            sent_msgs.append(msg)

        worker_ws.send_text = AsyncMock(side_effect=lambda s: sent_msgs.append(decode_control_payload(s)))

        async with hub._lock:
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.hijack_owner = _make_ws()
            st.hijack_owner_expires_at = time.monotonic() + 300

        result = await hub.force_release_hijack("w1")
        assert result is True, f"Expected True when hijack cleared, got {result!r}"

        assert len(sent_msgs) >= 1, f"Expected control message to be sent, got {sent_msgs}"
        ctrl = sent_msgs[0]
        assert ctrl.get("type") == "control", f"Expected type='control', got type={ctrl.get('type')!r}"
        assert ctrl.get("action") == "resume", f"Expected action='resume', got action={ctrl.get('action')!r}"
        assert "owner" in ctrl, f"'owner' key must be present in control msg, got {ctrl}"
        assert ctrl.get("owner") == "server-forced", (
            f"Expected owner='server-forced' (no REST session), got owner={ctrl.get('owner')!r}"
        )
        assert ctrl.get("lease_s") == 0, f"Expected lease_s=0, got lease_s={ctrl.get('lease_s')!r}"

    async def test_hijack_owner_expires_at_cleared_to_none(self) -> None:
        """hijack_owner_expires_at must be set to None (not empty string) after force release.

        Kills _mutmut_17: hijack_owner_expires_at=None → ''.
        """
        hub = _make_hub()
        worker_ws = AsyncMock()

        async with hub._lock:
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.hijack_owner = _make_ws()
            st.hijack_owner_expires_at = time.monotonic() + 300

        await hub.force_release_hijack("w1")

        async with hub._lock:
            st2 = hub.registry._workers.get("w1")
            if st2 is not None:
                assert st2.hijack_owner_expires_at is None, (
                    f"hijack_owner_expires_at must be None after force release, got {st2.hijack_owner_expires_at!r}"
                )

    async def test_hijack_state_broadcast_after_release(self) -> None:
        """broadcast_hijack_state is called with the correct worker_id (not None).

        Kills _mutmut_47: broadcast_hijack_state(worker_id) → broadcast_hijack_state(None).
        After release, browsers should receive an updated hijack_state with no owner.
        """
        hub = _make_hub()
        worker_ws = AsyncMock()
        browser_ws = AsyncMock()
        browser_ws.send_text = AsyncMock()

        async with hub._lock:
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.hijack_owner = _make_ws()
            st.hijack_owner_expires_at = time.monotonic() + 300
            st.browsers[browser_ws] = "admin"

        await hub.force_release_hijack("w1")

        # Browser should have received at least one message (the hijack_state update)
        assert browser_ws.send_text.called, "browser should receive hijack_state after force release"


# ---------------------------------------------------------------------------
# can_send_input — expired hijack owner gets 'and' not 'or' semantics
# ---------------------------------------------------------------------------


class TestCanSendInputHijackMode:
    def test_expired_hijack_owner_cannot_send(self) -> None:
        """In hijack mode with expired lease, hijack owner cannot send input.

        Kills _mutmut_16: 'and' → 'or'.
        Setup: hijack_owner=ws but hijack is EXPIRED → is_dashboard_hijack_active=False.
        With 'and': False AND True = False (correct — expired hijack blocks input).
        With 'or': False OR True = True (wrong — expired hijack owner could still send).
        """
        hub = _make_hub()
        ws = _make_ws()

        st = WorkerTermState()
        st.input_mode = "hijack"
        st.hijack_owner = ws
        # Set expires_at in the past to make hijack expired
        st.hijack_owner_expires_at = time.monotonic() - 10.0  # expired 10 seconds ago

        result = hub.can_send_input(st, ws)
        assert result is False, f"Expired hijack owner must NOT be able to send input, got {result!r}"


# ---------------------------------------------------------------------------
# request_analysis — message dict key names
# ---------------------------------------------------------------------------


class TestRequestAnalysis:
    async def test_worker_receives_analyze_req_with_req_id(self) -> None:
        """Worker receives analyze_req message with type and req_id fields.

        Kills _mutmut_9 (XXreq_idXX), _mutmut_10 (REQ_ID), _mutmut_11, _mutmut_12, _mutmut_13.
        """
        hub = _make_hub()
        worker_ws = AsyncMock()
        sent_msgs: list[dict[str, Any]] = []
        worker_ws.send_text = AsyncMock(side_effect=lambda s: sent_msgs.append(decode_control_payload(s)))

        async with hub._lock:
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws

        await hub.request_analysis("w1")

        assert len(sent_msgs) == 1, f"Expected 1 message sent, got {len(sent_msgs)}"
        msg = sent_msgs[0]
        assert msg.get("type") == "analyze_req", f"Expected type='analyze_req', got {msg.get('type')!r}"
        assert "req_id" in msg, f"'req_id' key must be present, got keys={list(msg.keys())}"
        assert isinstance(msg["req_id"], str), f"req_id must be a string, got {type(msg['req_id'])}"
        assert "ts" in msg, f"'ts' key must be present, got keys={list(msg.keys())}"


# ---------------------------------------------------------------------------
# allow_rest_send_for — per-client separate buckets
# ---------------------------------------------------------------------------


class TestAllowRestSendForPerClient:
    def test_different_clients_have_separate_bucket_objects(self) -> None:
        """Two different client_ids must use separate token bucket objects.

        Kills _mutmut_4: setdefault(client_id, ...) → setdefault(None, ...).
        With the mutation, all clients share the same bucket (keyed by None).
        """
        hub = _make_hub()

        # Trigger bucket creation for both clients
        hub.allow_rest_send_for("client1")
        hub.allow_rest_send_for("client2")

        assert "client1" in hub.limiter.rest_send_per_client, "client1 should have its own bucket entry"
        assert "client2" in hub.limiter.rest_send_per_client, "client2 should have its own bucket entry"
        assert hub.limiter.rest_send_per_client["client1"] is not hub.limiter.rest_send_per_client["client2"], (
            "client1 and client2 must use different bucket objects (separate per-client limits)"
        )

    def test_per_client_bucket_is_keyed_by_client_id(self) -> None:
        """The per-client bucket is stored under the client_id key, not None.

        With _mutmut_4, the bucket is stored under None — so client_id keys won't be found.
        """
        hub = _make_hub()
        hub.allow_rest_send_for("myClient")
        assert "myClient" in hub.limiter.rest_send_per_client, "'myClient' must be a key in _rest_send_per_client"
        assert None not in hub.limiter.rest_send_per_client, "None must NOT be a key in _rest_send_per_client"


# ---------------------------------------------------------------------------
# _resolve_role_for_browser — metric value, error worker_id, "viewer" valid role
# ---------------------------------------------------------------------------


class TestResolveBrowserRole:
    async def test_metric_called_with_value_one(self) -> None:
        """on_metric is called with value=1, not None or 2.

        Kills _mutmut_25 (value=None) and _mutmut_30 (value=2).
        """
        metric_calls: list[tuple[str, int]] = []

        def _collect(name: str, value: int) -> None:
            metric_calls.append((name, value))

        async def _mock_wait_for(coro: Any, **_kw: Any) -> None:
            raise TimeoutError("mocked")

        hub = _make_hub(
            resolve_browser_role=lambda ws, wid: asyncio.get_running_loop().create_future(),
            on_metric=_collect,
        )
        with patch("asyncio.wait_for", side_effect=_mock_wait_for), pytest.raises(BrowserRoleResolutionError):
            await hub._resolve_role_for_browser(_make_ws(), "w1")

        timeout_calls = [(n, v) for n, v in metric_calls if n == "browser_role_resolution_timeout"]
        assert len(timeout_calls) == 1, f"Expected 1 metric call, got {timeout_calls}"
        assert timeout_calls[0][1] == 1, f"Metric value must be 1, got {timeout_calls[0][1]!r}"

    async def test_timeout_error_has_correct_worker_id(self) -> None:
        """BrowserRoleResolutionError from timeout carries the correct worker_id.

        Kills _mutmut_31: BrowserRoleResolutionError(worker_id) → BrowserRoleResolutionError(None).
        """

        async def _mock_wait_for(coro: Any, **_kw: Any) -> None:
            raise TimeoutError("mocked")

        hub = _make_hub(
            resolve_browser_role=lambda ws, wid: asyncio.get_running_loop().create_future(),
        )
        with (
            patch("asyncio.wait_for", side_effect=_mock_wait_for),
            pytest.raises(BrowserRoleResolutionError) as exc_info,
        ):
            await hub._resolve_role_for_browser(_make_ws(), "target-worker")

        err = exc_info.value
        assert err.args[0] == "target-worker", (
            f"BrowserRoleResolutionError must carry worker_id='target-worker', got {err.args[0]!r}"
        )

    async def test_generic_exception_error_has_correct_worker_id(self) -> None:
        """BrowserRoleResolutionError from generic exception carries the correct worker_id.

        Kills _mutmut_40: BrowserRoleResolutionError(worker_id) → BrowserRoleResolutionError(None).
        """

        def _failing_resolver(ws: Any, worker_id: str) -> None:
            raise RuntimeError("boom")

        hub = _make_hub(resolve_browser_role=_failing_resolver)
        with pytest.raises(BrowserRoleResolutionError) as exc_info:
            await hub._resolve_role_for_browser(_make_ws(), "target-worker2")

        err = exc_info.value
        assert err.args[0] == "target-worker2", (
            f"BrowserRoleResolutionError must carry worker_id='target-worker2', got {err.args[0]!r}"
        )

    async def test_resolver_returning_viewer_uses_viewer_role(self) -> None:
        """When resolver returns 'viewer', that role is honoured.

        Kills _mutmut_43 (replaces 'viewer' with 'XXviewerXX' in valid-role set)
        and _mutmut_44 (replaces 'viewer' with 'VIEWER').
        """
        hub = _make_hub(resolve_browser_role=lambda ws, wid: "viewer")
        ws = _make_ws()
        # Call _resolve_role_for_browser directly; it returns the resolved role
        role = await hub._resolve_role_for_browser(ws, "w1")
        assert role == "viewer", f"Expected 'viewer' role from resolver, got {role!r}"

    async def test_invalid_role_falls_back_to_viewer(self) -> None:
        """When resolver returns invalid role, fallback to 'viewer'.

        Kills _mutmut_49: 'is not None' → 'is None' (inverts the log-and-fallback logic).
        The function falls back to role='viewer' when resolved_role is not a valid string.
        """
        hub = _make_hub(resolve_browser_role=lambda ws, wid: "superadmin")  # invalid role
        ws = _make_ws()
        role = await hub._resolve_role_for_browser(ws, "w1")
        # 'superadmin' is not in valid roles, should fall back to 'viewer'
        assert role == "viewer", f"Invalid role must fall back to 'viewer', got {role!r}"


# ---------------------------------------------------------------------------
# _safe_int in hijack/models.py
# ---------------------------------------------------------------------------


class TestSafeIntModels:
    """Kill hijack.models._safe_int mutmut_6: result < min_val → result <= min_val.

    With the mutation, _safe_int(min_val, default, min_val=min_val) would return
    default even when result == min_val (the minimum valid value).
    The original only rejects when result < min_val; min_val itself is valid.
    """

    def test_safe_int_exact_min_val_is_accepted(self) -> None:
        """_safe_int(1, 80, min_val=1) must return 1, not the default.

        Kills mutmut_6: result < min_val → result <= min_val.
        With mutation: result=1 <= min_val=1 is True → returns default=80.
        With original: result=1 < min_val=1 is False → returns 1.
        """
        from provide.uterm.server.bridge.models import _safe_int

        assert _safe_int(1, 80, min_val=1) == 1, "_safe_int(1, 80, min_val=1) must return 1 (min_val is valid)"

    def test_safe_int_below_min_val_is_rejected(self) -> None:
        """_safe_int(0, 80, min_val=1) must return default=80."""
        from provide.uterm.server.bridge.models import _safe_int

        assert _safe_int(0, 80, min_val=1) == 80

    def test_safe_int_above_min_val_is_accepted(self) -> None:
        """_safe_int(2, 80, min_val=1) must return 2."""
        from provide.uterm.server.bridge.models import _safe_int

        assert _safe_int(2, 80, min_val=1) == 2

    def test_safe_int_no_min_val_accepts_any_int(self) -> None:
        """Without min_val, any valid int is accepted."""
        from provide.uterm.server.bridge.models import _safe_int

        assert _safe_int(-100, 0) == -100
