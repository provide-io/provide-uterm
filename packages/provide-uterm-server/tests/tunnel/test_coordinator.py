from provide.terminal.bridge.coordinator import HijackCoordinator


def test_coordinator_heartbeat_rejects_wrong_owner():
    coord = HijackCoordinator()
    res = coord.acquire("admin", 60)
    hijack_id = res.session.hijack_id

    # Heartbeat with WRONG owner should fail
    # Note: This will fail with TypeError until owner is added to heartbeat()
    hb_res = coord.heartbeat(hijack_id, 60, owner="attacker")
    assert hb_res.ok is False
    assert hb_res.error == "owner_mismatch"
