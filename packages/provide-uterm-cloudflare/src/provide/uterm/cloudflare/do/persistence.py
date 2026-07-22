#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Lease persistence helpers for SessionRuntime.

Extracted from ``session_runtime.py`` to keep file size under 500 LOC.
Provides ``persist_lease`` and ``clear_lease`` as module-level functions
so they can be tested independently of the full Durable Object runtime.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def schedule_alarm(ctx: Any, wall_seconds: float) -> None:
    """Schedule a Durable Object alarm at *wall_seconds* (wall-clock epoch).

    Guards against a ``ctx`` whose ``storage`` is absent or lacks a callable
    ``setAlarm`` (e.g. local pywrangler dev), in which case scheduling is a
    no-op. The alarm time is passed in milliseconds, as the CF runtime expects.
    """
    _s = getattr(ctx, "storage", None)
    if _s is not None and callable(getattr(_s, "setAlarm", None)):
        _s.setAlarm(int(wall_seconds * 1000))


def persist_lease(
    store: Any,
    ctx: Any,
    worker_id: str,
    session: Any,
    lease_record_cls: Any,
) -> None:
    """Persist a hijack lease to SQLite and schedule an alarm for its expiry.

    Args:
        store: ``SqliteStateStore`` instance.
        ctx: Durable Object ``ctx`` (used to access ``ctx.storage.setAlarm``).
        worker_id: The current worker ID.
        session: ``HijackSession`` to persist, or ``None`` (no-op).
        lease_record_cls: ``LeaseRecord`` class used to construct the record.
    """
    if session is None:
        return
    # session.lease_expires_at is a monotonic timestamp; persist it as wall-clock
    # so it survives DO restart/hibernation (monotonic clocks reset per isolate).
    wall_expires = session.lease_expires_at + (time.time() - time.monotonic())
    store.save_lease(
        lease_record_cls(
            worker_id=worker_id,
            hijack_id=session.hijack_id,
            owner=session.owner,
            lease_expires_at=wall_expires,
        )
    )
    schedule_alarm(ctx, wall_expires)


def clear_lease(store: Any, worker_id: str) -> None:
    """Clear the persisted lease record for *worker_id*.

    Args:
        store: ``SqliteStateStore`` instance.
        worker_id: The worker whose lease should be removed.
    """
    store.clear_lease(worker_id)
