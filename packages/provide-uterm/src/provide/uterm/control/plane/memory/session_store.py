#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from provide.uterm.control.plane.memory.transaction import MemoryState, MemoryTransaction
    from provide.uterm.control.plane.session.types import SessionRecord


class MemorySessionStore:
    def __init__(self, state: MemoryState, tx: MemoryTransaction) -> None:
        self._state = state
        self._tx = tx

    async def upsert_session(self, record: SessionRecord) -> None:
        self._state.sessions[record.session_id] = record

    async def get_session(self, session_id: str) -> SessionRecord | None:
        return self._state.sessions.get(session_id)

    async def mark_deleted(self, session_id: str, deleted_at: float) -> None:
        current = self._state.sessions.get(session_id)
        if current is None:
            return
        self._state.sessions[session_id] = replace(current, deleted_at=deleted_at, lifecycle_state="deleted")
