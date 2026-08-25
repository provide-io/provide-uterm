#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit coverage for the telnet e2e drain helpers themselves.

``drain_for_snapshot_with_text`` polls in short slices so it can re-check its
own deadline between snapshots.  A slice that ends without a snapshot means
"nothing arrived in the last half second", not "the stream is finished" -- so
the outer wait has to keep going until *its* deadline.  Treating an empty slice
as terminal silently collapses a ten-second budget down to one 0.5s window,
which passes whenever the first window happens to catch a frame and fails when
the runner is starved.
"""

from __future__ import annotations

import asyncio
from typing import Any

from provide.uterm.control_channel import encode_control_frame

from .conftest import _WS_DECODERS, _WS_PENDING, drain_for_snapshot_with_text

BANNER = "ECHO_BANNER"


class _QuietThenBanner:
    """A websocket-ish double that stalls for N receives, then yields a snapshot."""

    def __init__(self, quiet_receives: int) -> None:
        self._receives = 0
        self._quiet_receives = quiet_receives
        self._frame = encode_control_frame({"type": "snapshot", "screen": f"line\n{BANNER}\n"})

    async def recv(self) -> str:
        self._receives += 1
        if self._receives <= self._quiet_receives:
            # Never resolves; the caller's wait_for cancels us and moves on.
            await asyncio.Event().wait()
        return self._frame


async def test_a_quiet_slice_does_not_abandon_the_outer_deadline() -> None:
    """A silent first slice must not end a wait that still has seconds left."""
    ws: Any = _QuietThenBanner(quiet_receives=2)
    try:
        snap = await drain_for_snapshot_with_text(ws, BANNER, timeout=5.0)
    finally:
        _WS_PENDING.pop(ws, None)
        _WS_DECODERS.pop(ws, None)

    assert snap is not None, "a quiet 0.5s slice ended the wait early"
    assert BANNER in snap["screen"]
