#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the HijackLease value object on WorkerTermState.

The lease is a read-only view over the three hijack fields; mutations go
through ``apply_lease``. State-machine semantics (idle / dashboard /
REST / expired) live on the view, so they can be reasoned about and
tested without booting a full hub.
"""

from __future__ import annotations

from provide.uterm.bridge.coordinator import HijackSession
from provide.uterm.bridge.models import HijackLease, WorkerTermState


class _FakeWebSocket:
    """Sentinel object — HijackLease only compares by identity."""


class TestHijackLeaseIdle:
    def test_default_is_idle(self) -> None:
        assert HijackLease().is_idle

    def test_neither_active_at_any_time(self) -> None:
        lease = HijackLease()
        assert not lease.is_active(now=0.0)
        assert not lease.is_active(now=1e9)

    def test_state_default_lease_is_idle(self) -> None:
        st = WorkerTermState()
        assert st.lease.is_idle


class TestHijackLeaseDashboardPath:
    def _make(self) -> tuple[_FakeWebSocket, HijackLease]:
        ws = _FakeWebSocket()
        lease = HijackLease(ws=ws, ws_expires_at=100.0)
        return ws, lease

    def test_dashboard_active_before_expiry(self) -> None:
        _ws, lease = self._make()
        assert lease.is_dashboard_active(now=50.0)
        assert lease.is_active(now=50.0)
        assert not lease.is_rest_active(now=50.0)
        assert not lease.is_idle

    def test_dashboard_inactive_after_expiry(self) -> None:
        _ws, lease = self._make()
        assert not lease.is_dashboard_active(now=100.0)  # boundary: inclusive
        assert not lease.is_dashboard_active(now=101.0)

    def test_dashboard_inactive_without_expiry_field(self) -> None:
        lease = HijackLease(ws=_FakeWebSocket(), ws_expires_at=None)
        assert not lease.is_dashboard_active(now=0.0)


class TestHijackLeaseRestPath:
    def _make_session(self, expires_at: float = 100.0) -> HijackSession:
        return HijackSession(
            hijack_id="hj-1",
            owner="alice",
            acquired_at=0.0,
            lease_expires_at=expires_at,
            last_heartbeat=0.0,
        )

    def test_rest_active_before_expiry(self) -> None:
        lease = HijackLease(session=self._make_session(100.0))
        assert lease.is_rest_active(now=50.0)
        assert lease.is_active(now=50.0)
        assert not lease.is_dashboard_active(now=50.0)
        assert not lease.is_idle

    def test_rest_inactive_after_expiry(self) -> None:
        lease = HijackLease(session=self._make_session(100.0))
        assert not lease.is_rest_active(now=100.0)
        assert not lease.is_rest_active(now=101.0)


class TestHijackLeaseExpire:
    def test_expire_idle_lease_is_noop(self) -> None:
        lease = HijackLease()
        rest_expired, dash_expired = lease.expire(now=0.0)
        assert rest_expired is False
        assert dash_expired is False

    def test_expire_clears_stale_dashboard(self) -> None:
        ws = _FakeWebSocket()
        lease = HijackLease(ws=ws, ws_expires_at=10.0)
        _, dash_expired = lease.expire(now=20.0)
        assert dash_expired is True
        assert lease.ws is None
        assert lease.ws_expires_at is None

    def test_expire_preserves_fresh_dashboard(self) -> None:
        ws = _FakeWebSocket()
        lease = HijackLease(ws=ws, ws_expires_at=100.0)
        _, dash_expired = lease.expire(now=50.0)
        assert dash_expired is False
        assert lease.ws is ws

    def test_expire_clears_stale_rest_session(self) -> None:
        sess = HijackSession(
            hijack_id="hj",
            owner="o",
            acquired_at=0.0,
            lease_expires_at=10.0,
            last_heartbeat=0.0,
        )
        lease = HijackLease(session=sess)
        rest_expired, _ = lease.expire(now=20.0)
        assert rest_expired is True
        assert lease.session is None

    def test_expire_can_clear_both_paths(self) -> None:
        ws = _FakeWebSocket()
        sess = HijackSession(
            hijack_id="hj",
            owner="o",
            acquired_at=0.0,
            lease_expires_at=10.0,
            last_heartbeat=0.0,
        )
        lease = HijackLease(ws=ws, ws_expires_at=10.0, session=sess)
        rest_expired, dash_expired = lease.expire(now=20.0)
        assert (rest_expired, dash_expired) == (True, True)
        assert lease.is_idle


class TestWorkerTermStateLeaseRoundTrip:
    def test_apply_lease_writes_back(self) -> None:
        st = WorkerTermState()
        ws = _FakeWebSocket()
        new_lease = HijackLease(ws=ws, ws_expires_at=99.0)
        st.apply_lease(new_lease)
        assert st.hijack_owner is ws
        assert st.hijack_owner_expires_at == 99.0

    def test_lease_view_reflects_current_state(self) -> None:
        ws = _FakeWebSocket()
        st = WorkerTermState(hijack_owner=ws, hijack_owner_expires_at=99.0)
        lease = st.lease
        assert lease.ws is ws
        assert lease.ws_expires_at == 99.0
        assert lease.is_dashboard_active(now=50.0)

    def test_lease_view_is_a_snapshot_not_a_reference(self) -> None:
        """Mutations to the returned view do NOT propagate back; that's
        what ``apply_lease`` is for. Guards against accidental
        side-effects when callers pass the lease around."""
        st = WorkerTermState(hijack_owner=_FakeWebSocket(), hijack_owner_expires_at=99.0)
        view = st.lease
        view.ws = None
        # State unchanged because view is a separate object.
        assert st.hijack_owner is not None
