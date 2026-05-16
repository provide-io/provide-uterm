#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from provide.uterm.control.plane.session.types import SessionRecord


class SessionStore(Protocol):
    async def upsert_session(self, record: SessionRecord) -> None: ...

    async def get_session(self, session_id: str) -> SessionRecord | None: ...

    async def mark_deleted(self, session_id: str, deleted_at: float) -> None: ...
