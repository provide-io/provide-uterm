#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from provide.uterm.client.control_ws import (
        AsyncInlineWebSocketClient,
        LogicalFrameDecoder,
        SyncInlineWebSocketClient,
        connect_async_ws,
        connect_test_ws,
    )
    from provide.uterm.client.hijack import HijackClient

__all__ = [
    "AsyncInlineWebSocketClient",
    "HijackClient",
    "LogicalFrameDecoder",
    "SyncInlineWebSocketClient",
    "connect_async_ws",
    "connect_test_ws",
]


def __getattr__(name: str) -> object:
    if name in {
        "AsyncInlineWebSocketClient",
        "LogicalFrameDecoder",
        "SyncInlineWebSocketClient",
        "connect_async_ws",
        "connect_test_ws",
    }:
        from provide.uterm.client import control_ws

        return getattr(control_ws, name)
    if name == "HijackClient":
        from provide.uterm.client.hijack import HijackClient

        return HijackClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
