#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Focused contracts for live-server EventBus synchronization helpers."""

from __future__ import annotations

from types import SimpleNamespace

from provide.uterm.server.bridge.hub import EventBus

from ._live_server import wait_for_subscribers


async def test_wait_for_subscribers_defaults_to_public_event_bus() -> None:
    public_bus = EventBus()
    operation_bus = EventBus()
    hub = SimpleNamespace(event_bus=public_bus, _operation_event_bus=operation_bus)

    async with public_bus.watch("w1"):
        await wait_for_subscribers(hub, "w1", 1, timeout=0.05, interval=0.001)

    assert operation_bus.subscriber_count("w1") == 0


async def test_wait_for_subscribers_can_select_private_operation_bus() -> None:
    public_bus = EventBus()
    operation_bus = EventBus()
    hub = SimpleNamespace(event_bus=public_bus, _operation_event_bus=operation_bus)

    async with operation_bus.watch("w1"):
        await wait_for_subscribers(hub, "w1", 1, stream="operation", timeout=0.05, interval=0.001)

    assert public_bus.subscriber_count("w1") == 0
