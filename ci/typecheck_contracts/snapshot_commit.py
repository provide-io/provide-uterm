#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Static consumer contracts for snapshot commit overloads."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, assert_type

if TYPE_CHECKING:
    from fastapi import WebSocket

    from provide.uterm.server.bridge.hub import TermHub, TermHubProtocol
    from provide.uterm.server.bridge.hub.router import MessageRouter


async def check_snapshot_commit_overloads(
    hub: TermHub,
    protocol: TermHubProtocol,
    router: MessageRouter,
    worker: WebSocket,
    optional_worker: WebSocket | None,
    snapshot: dict[str, Any],
) -> None:
    for compatibility_subject in (hub, protocol, router):
        assert_type(
            await compatibility_subject.commit_snapshot_event("w1", snapshot),
            dict[str, Any],
        )
        assert_type(
            await compatibility_subject.commit_snapshot_event("w1", snapshot, expected_worker=None),
            dict[str, Any],
        )
        assert_type(
            await compatibility_subject.commit_snapshot_event("w1", snapshot, expected_worker=worker),
            dict[str, Any] | None,
        )
        assert_type(
            await compatibility_subject.commit_snapshot_event("w1", snapshot, expected_worker=optional_worker),
            dict[str, Any] | None,
        )
