#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A reconnecting session keeps the close that made it reconnect.

``ReconnectingSession`` rebuilds a dropped session transparently; before this,
the reason the old session ended was gone the moment it was replaced.
"""

from __future__ import annotations

import pytest
from provide.uterm.transport_close import CloseInitiator, TransportClose, TransportClosedError

from provide.uterm.transports import reconnect

_GOING_AWAY = TransportClose(CloseInitiator.REMOTE, code=1001, reason="going away")


class _Session:
    def __init__(self, *, drops: bool) -> None:
        self.drops = drops
        self.close_info: TransportClose | None = None
        self.closed = False

    def is_connected(self) -> bool:
        return not self.closed

    async def close(self) -> None:
        self.closed = True

    async def send(self, data: str) -> None:
        if self.drops:
            self.close_info = _GOING_AWAY
            self.drops = False
            raise TransportClosedError("Connection closed", _GOING_AWAY)


async def test_the_close_that_caused_a_reconnect_is_kept() -> None:
    sessions = [_Session(drops=True), _Session(drops=False)]

    async def connect() -> _Session:
        return sessions.pop(0)

    rs = await reconnect.connect_with_reconnect(connect, policy=reconnect.ReconnectPolicy(base_backoff_s=0))
    assert rs.last_close is None

    await rs.send("hello")

    assert rs.last_close == _GOING_AWAY


@pytest.mark.parametrize("close_info", [None])
async def test_a_session_that_never_closed_leaves_no_last_close(close_info: TransportClose | None) -> None:
    async def connect() -> _Session:
        return _Session(drops=False)

    rs = await reconnect.connect_with_reconnect(connect, policy=reconnect.ReconnectPolicy(base_backoff_s=0))
    await rs.reconnect()

    assert rs.last_close is close_info
