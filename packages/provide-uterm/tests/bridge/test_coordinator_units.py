#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit-level branch coverage for HijackCoordinator.

The stress tests exercise success paths; these tests target the error and
edge-case branches (mismatched hijack id, missing session, owner mismatch,
lease clamping, owner-mismatch on acquire of an active session, ...) so the
core coverage gate stays at 100% with the coordinator now living in core.
"""

from __future__ import annotations

import time

from provide.uterm.bridge.coordinator import (
    AcquireResult,
    HijackCoordinator,
    HijackSession,
)


def test_dataclass_construction() -> None:
    """HijackSession and AcquireResult are plain dataclasses with sane defaults."""
    s = HijackSession(hijack_id="h", owner="o", lease_expires_at=1.0)
    assert s.acquired_at == 0.0
    assert s.last_heartbeat == 0.0

    r = AcquireResult(ok=True, session=s)
    assert r.error is None
    assert r.is_renewal is False


def test_session_property_returns_none_when_unset() -> None:
    """Empty coordinator → session is None."""
    coord = HijackCoordinator()
    assert coord.session is None


def test_acquire_blocks_different_owner() -> None:
    """A second owner trying to acquire while a lease is active is rejected."""
    coord = HijackCoordinator()
    first = coord.acquire("alice", 60)
    assert first.ok

    rejected = coord.acquire("bob", 60)
    assert rejected.ok is False  # identity, not just falsy — pins ok=False vs ok=None
    assert rejected.error == "already_hijacked"
    assert rejected.session is not None
    assert rejected.session.owner == "alice"


def test_acquire_same_owner_renews_with_new_id() -> None:
    """Same owner re-acquiring receives a new hijack_id and is flagged as renewal."""
    coord = HijackCoordinator()
    first = coord.acquire("alice", 60)
    assert first.ok and first.session is not None

    renewed = coord.acquire("alice", 60)
    assert renewed.ok
    assert renewed.is_renewal
    assert renewed.session is not None
    assert renewed.session.hijack_id != first.session.hijack_id


def test_acquire_after_expiry_starts_fresh() -> None:
    """An expired lease is cleared and a fresh acquire is not a renewal."""
    coord = HijackCoordinator()
    now = time.monotonic()
    coord.acquire("alice", 1, now=now - 10)

    fresh = coord.acquire("alice", 60, now=now)
    assert fresh.ok
    assert fresh.is_renewal is False


def test_acquire_clamps_lease_duration() -> None:
    """Lease durations outside [1, 3600] are clamped on acquire."""
    coord = HijackCoordinator()
    now = 1_000.0
    low = coord.acquire("a", 0, now=now)
    assert low.session is not None
    assert low.session.lease_expires_at == now + 1
    # acquire stamps both timestamps from now (pins acquired_at / last_heartbeat).
    assert low.session.acquired_at == now
    assert low.session.last_heartbeat == now

    coord_high = HijackCoordinator()
    high = coord_high.acquire("a", 10_000, now=now)
    assert high.session is not None
    assert high.session.lease_expires_at == now + 3600


def test_heartbeat_no_active_session() -> None:
    """heartbeat() when not hijacked → error='not_hijacked'."""
    coord = HijackCoordinator()
    result = coord.heartbeat("fake-id", 60, "owner")
    assert result.ok is False
    assert result.session is None
    assert result.error == "not_hijacked"


def test_heartbeat_wrong_hijack_id() -> None:
    """heartbeat() with wrong hijack_id → error='hijack_id_mismatch'."""
    coord = HijackCoordinator()
    acquire = coord.acquire("owner", 60)
    assert acquire.ok and acquire.session is not None
    result = coord.heartbeat("wrong-id", 60, "owner")
    assert result.ok is False
    assert result.error == "hijack_id_mismatch"
    # The rejection echoes the live session, not None (pins session=active).
    assert result.session is not None
    assert result.session.hijack_id == acquire.session.hijack_id


def test_heartbeat_wrong_owner() -> None:
    """heartbeat() with mismatched owner → error='owner_mismatch'."""
    coord = HijackCoordinator()
    acquire = coord.acquire("alice", 60)
    assert acquire.ok and acquire.session is not None
    result = coord.heartbeat(acquire.session.hijack_id, 60, "bob")
    assert result.ok is False
    assert result.error == "owner_mismatch"
    # The rejection echoes the live session, not None (pins session=active).
    assert result.session is not None
    assert result.session.hijack_id == acquire.session.hijack_id


def test_heartbeat_without_owner_arg_succeeds() -> None:
    """heartbeat() with owner=None skips owner check and refreshes the lease."""
    coord = HijackCoordinator()
    start = 5_000.0
    acquire = coord.acquire("alice", 60, now=start)
    assert acquire.ok and acquire.session is not None
    beat = start + 30
    result = coord.heartbeat(acquire.session.hijack_id, 60, now=beat)
    assert result.ok is True
    # Success returns the live session (pins session=active) ...
    assert result.session is not None
    assert result.session.hijack_id == acquire.session.hijack_id
    # ... with both lease + heartbeat advanced to the heartbeat instant.
    assert result.session.last_heartbeat == beat
    assert result.session.lease_expires_at == beat + 60


def test_release_no_active_session() -> None:
    """release() when not hijacked → error='not_hijacked'."""
    coord = HijackCoordinator()
    result = coord.release("fake-id")
    assert result.ok is False
    assert result.session is None
    assert result.error == "not_hijacked"


def test_release_wrong_hijack_id() -> None:
    """release() with wrong hijack_id → error='hijack_id_mismatch'."""
    coord = HijackCoordinator()
    acquire = coord.acquire("owner", 60)
    assert acquire.ok and acquire.session is not None
    result = coord.release("wrong-id")
    assert result.ok is False
    assert result.error == "hijack_id_mismatch"
    # A mismatched release echoes the live session, not None (pins session=active).
    assert result.session is not None
    assert result.session.hijack_id == acquire.session.hijack_id


def test_release_success_clears_session() -> None:
    """release() with correct id clears the active lease."""
    coord = HijackCoordinator()
    acquire = coord.acquire("owner", 60)
    assert acquire.ok and acquire.session is not None
    result = coord.release(acquire.session.hijack_id)
    assert result.ok
    assert result.session is None
    assert coord.session is None


def test_can_send_input_no_active_session() -> None:
    """can_send_input() when not hijacked → False for any id (and None)."""
    coord = HijackCoordinator()
    assert coord.can_send_input(None) is False
    assert coord.can_send_input("some-id") is False


def test_can_send_input_wrong_and_right_ids() -> None:
    """can_send_input() returns True only for the active hijack_id."""
    coord = HijackCoordinator()
    acquire = coord.acquire("owner", 60)
    assert acquire.ok and acquire.session is not None
    assert coord.can_send_input("wrong-id") is False
    assert coord.can_send_input(acquire.session.hijack_id) is True


def test_session_property_returns_active() -> None:
    """The .session property returns the active HijackSession when one exists."""
    coord = HijackCoordinator()
    acquire = coord.acquire("owner", 60)
    assert acquire.session is not None
    active = coord.session
    assert active is not None
    assert active.hijack_id == acquire.session.hijack_id


def test_session_property_clears_expired() -> None:
    """The .session property purges an expired lease lazily."""
    coord = HijackCoordinator()
    past = time.monotonic() - 100
    coord.acquire("owner", 1, now=past)
    # By now (real monotonic), the lease has long expired.
    assert coord.session is None


def test_active_session_expired_at_exact_boundary() -> None:
    """A lease is expired the instant now == lease_expires_at (pins ``<=`` vs ``<``).

    Each coordinator is probed exactly once: a heartbeat that finds a live lease
    *renews* it, so the just-before and at-boundary probes must not share state.
    """
    start = 1_000.0
    # Just before the boundary the lease is still live.
    live_coord = HijackCoordinator()
    acq = live_coord.acquire("owner", 1, now=start)
    assert acq.session is not None
    boundary = acq.session.lease_expires_at  # exactly start + 1
    live = live_coord.heartbeat(acq.session.hijack_id, 1, now=boundary - 0.001)
    assert live.ok is True
    # At exactly the boundary it is gone — `<` would keep it alive.
    exp_coord = HijackCoordinator()
    acq2 = exp_coord.acquire("owner", 1, now=start)
    assert acq2.session is not None
    expired = exp_coord.heartbeat(acq2.session.hijack_id, 1, now=boundary)
    assert expired.ok is False
    assert expired.error == "not_hijacked"


def test_active_session_clear_resets_slot_to_none() -> None:
    """Expiry must reset the slot to None, so a second read stays clean.

    A mutant that clears the slot to a non-None falsy value (e.g. "") would
    survive the first read but crash on the second when it dereferences a str.
    """
    coord = HijackCoordinator()
    start = 2_000.0
    acq = coord.acquire("owner", 1, now=start)
    assert acq.session is not None
    after = start + 10
    # First read past expiry clears the slot...
    assert coord.heartbeat(acq.session.hijack_id, 1, now=after).error == "not_hijacked"
    # ...and a second read is still well-behaved (not an AttributeError on "").
    assert coord.heartbeat(acq.session.hijack_id, 1, now=after).error == "not_hijacked"
    assert coord.session is None
