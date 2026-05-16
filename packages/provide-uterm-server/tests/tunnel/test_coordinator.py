#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from provide.uterm.bridge.coordinator import HijackCoordinator


def test_coordinator_heartbeat_rejects_wrong_owner():
    coord = HijackCoordinator()
    res = coord.acquire("admin", 60)
    hijack_id = res.session.hijack_id

    # Heartbeat with WRONG owner should fail
    # Note: This will fail with TypeError until owner is added to heartbeat()
    hb_res = coord.heartbeat(hijack_id, 60, owner="attacker")
    assert hb_res.ok is False
    assert hb_res.error == "owner_mismatch"
