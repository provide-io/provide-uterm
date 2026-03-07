from __future__ import annotations

from provide_terminal_cloudflare.bridge.hijack import HijackCoordinator


def test_acquire_conflict_and_release() -> None:
    hub = HijackCoordinator()
    first = hub.acquire("alice", 60, now=100.0)
    assert first.ok is True
    assert first.session is not None

    second = hub.acquire("bob", 60, now=101.0)
    assert second.ok is False
    assert second.error == "already_hijacked"

    released = hub.release(first.session.hijack_id)
    assert released.ok is True

    third = hub.acquire("bob", 60, now=102.0)
    assert third.ok is True


def test_heartbeat_mismatch() -> None:
    hub = HijackCoordinator()
    acquired = hub.acquire("alice", 60, now=100.0)
    assert acquired.ok is True
    result = hub.heartbeat("wrong", 60, now=101.0)
    assert result.ok is False
    assert result.error == "hijack_id_mismatch"
