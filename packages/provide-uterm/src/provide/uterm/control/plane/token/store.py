#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from provide.uterm.control.plane.token.types import ResumeTokenRecord, SessionTokenRecord


class TokenStore(Protocol):
    async def put_session_token(self, record: SessionTokenRecord) -> None: ...

    async def get_session_token(self, session_id: str, token_kind: str) -> SessionTokenRecord | None: ...

    async def create_resume_token(self, record: ResumeTokenRecord) -> None: ...

    async def get_resume_token(self, token_value: str) -> ResumeTokenRecord | None: ...

    async def revoke_resume_token(self, token_value: str, revoked_at: float) -> None: ...

    async def consume_resume_token(self, token_value: str, revoked_at: float) -> ResumeTokenRecord | None: ...
