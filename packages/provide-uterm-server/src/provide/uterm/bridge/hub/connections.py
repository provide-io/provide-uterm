#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Legacy re-exports from the hub connections module.

Phase 7b of refactor #16 collapsed the ``_ConnectionMixin`` class into
:class:`provide.uterm.bridge.hub.core.TermHub` (forwards to
:class:`ConnectionManager` and :class:`PresenceManager`). This module
remains because three symbols are still imported by external callers:

* ``_REST_CLIENT_CACHE_MAX`` / ``_REST_CLIENT_EVICT_COUNT`` —
  test-only constants re-exported from
  :mod:`provide.uterm.bridge.hub.limiter` so existing
  ``from provide.uterm.bridge.hub.connections import ...`` imports keep
  working.
* :func:`shutdown_background_tasks` — used by the store and by
  ``tests/bridge/test_core_fixes_2.py`` to drive graceful shutdown.

Canonical definitions for the rate-limit constants live in the limiter
module; this module is purely a back-compat shim.
"""

from __future__ import annotations

import asyncio
from typing import Any

from provide.uterm.bridge.hub.limiter import (
    REST_CLIENT_CACHE_MAX as _REST_CLIENT_CACHE_MAX,
)
from provide.uterm.bridge.hub.limiter import (
    REST_CLIENT_EVICT_COUNT as _REST_CLIENT_EVICT_COUNT,
)


async def shutdown_background_tasks(task_set: set[asyncio.Task[Any]]) -> int:
    """Cancel and await all pending background tasks. Returns count cancelled."""
    tasks = list(task_set)
    if not tasks:
        return 0
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    task_set.clear()
    return sum(1 for r in results if isinstance(r, (asyncio.CancelledError, Exception)))


__all__ = ["_REST_CLIENT_CACHE_MAX", "_REST_CLIENT_EVICT_COUNT", "shutdown_background_tasks"]
