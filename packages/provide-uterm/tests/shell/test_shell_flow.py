#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""UshellConnector Tier-A backpressure: flow_pause/flow_resume + snapshot_request."""

from __future__ import annotations

from provide.uterm.shell.terminal import UshellConnector


async def test_init_flow_paused_defaults_false() -> None:
    assert UshellConnector("s")._flow_paused is False


async def test_handle_control_flow_pause_resume_toggles_flag() -> None:
    conn = UshellConnector("s")
    assert await conn.handle_control("flow_pause") == []
    assert conn._flow_paused is True
    assert await conn.handle_control("flow_resume") == []
    assert conn._flow_paused is False


async def test_handle_control_snapshot_request_returns_snapshot() -> None:
    conn = UshellConnector("s")
    out = await conn.handle_control("snapshot_request")
    assert len(out) == 1
    assert out[0]["type"] == "snapshot"


async def test_handle_control_hijack_actions_do_not_set_flow_paused() -> None:
    conn = UshellConnector("s")
    assert await conn.handle_control("pause") == []
    assert conn._flow_paused is False


async def test_poll_withholds_pending_while_flow_paused_then_delivers() -> None:
    conn = UshellConnector("s")
    await conn.start()
    await conn.poll_messages()  # consume the welcome → _welcomed True
    conn._pending_frames = [{"type": "term", "data": "x"}]
    await conn.handle_control("flow_pause")
    assert await conn.poll_messages() == []  # withheld while flow-paused
    await conn.handle_control("flow_resume")
    assert await conn.poll_messages() == [{"type": "term", "data": "x"}]  # delivered after resume
