#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Fan-out feature: broadcast input to multiple terminal sessions."""

from __future__ import annotations

from provide.uterm.bridge.fanout._controller import FanOutController
from provide.uterm.bridge.fanout._models import FanOutGroup, FanOutResult, SessionFanOutResult
from provide.uterm.bridge.fanout._store import FanOutStore, InMemoryFanOutStore

__all__ = [
    "FanOutController",
    "FanOutGroup",
    "FanOutResult",
    "FanOutStore",
    "InMemoryFanOutStore",
    "SessionFanOutResult",
]
