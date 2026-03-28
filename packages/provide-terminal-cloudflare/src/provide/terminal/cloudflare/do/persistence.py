#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Lease persistence helpers for SessionRuntime.

Extracted from ``session_runtime.py`` to keep file size under 500 LOC.
Provides ``persist_lease`` and ``clear_lease`` as module-level functions
so they can be tested independently of the full Durable Object runtime.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


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
    store.save_lease(
        lease_record_cls(
            worker_id=worker_id,
            hijack_id=session.hijack_id,
            owner=session.owner,
            lease_expires_at=session.lease_expires_at,
        )
    )
    _s = getattr(ctx, "storage", None)
    if _s is not None and callable(getattr(_s, "setAlarm", None)):
        _s.setAlarm(int(session.lease_expires_at * 1000))


def clear_lease(store: Any, worker_id: str) -> None:
    """Clear the persisted lease record for *worker_id*.

    Args:
        store: ``SqliteStateStore`` instance.
        worker_id: The worker whose lease should be removed.
    """
    store.clear_lease(worker_id)
