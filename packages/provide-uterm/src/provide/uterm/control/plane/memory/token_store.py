#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from provide.uterm.control.plane.memory.transaction import MemoryState, MemoryTransaction
    from provide.uterm.control.plane.token.types import ResumeTokenRecord, SessionTokenRecord


class MemoryTokenStore:
    def __init__(self, state: MemoryState, tx: MemoryTransaction) -> None:
        self._state = state
        self._tx = tx

    async def put_session_token(self, record: SessionTokenRecord) -> None:
        self._state.session_tokens[(record.session_id, record.token_kind)] = record

    async def get_session_token(self, session_id: str, token_kind: str) -> SessionTokenRecord | None:
        return self._state.session_tokens.get((session_id, token_kind))

    async def create_resume_token(self, record: ResumeTokenRecord) -> None:
        self._state.resume_tokens[record.token_value] = record

    async def get_resume_token(self, token_value: str) -> ResumeTokenRecord | None:
        record = self._state.resume_tokens.get(token_value)
        if record is None or record.revoked_at is not None:
            return None
        return record

    async def revoke_resume_token(self, token_value: str, revoked_at: float) -> None:
        record = self._state.resume_tokens.get(token_value)
        if record is None:
            return
        self._state.resume_tokens[token_value] = replace(record, revoked_at=revoked_at)

    async def consume_resume_token(self, token_value: str, revoked_at: float) -> ResumeTokenRecord | None:
        record = self._state.resume_tokens.get(token_value)
        if record is None or record.revoked_at is not None:
            return None
        self._state.resume_tokens[token_value] = replace(record, revoked_at=revoked_at)
        return record
