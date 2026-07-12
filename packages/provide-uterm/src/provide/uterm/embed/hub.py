#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""In-process embed session factory."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from provide.uterm.embed.session import EmbedSession

if TYPE_CHECKING:
    from collections.abc import Mapping

    from provide.uterm.embed.session import ByteInterceptor
    from provide.uterm.embed.types import TelnetPolicy

__all__ = ["EmbedHub"]


class EmbedHub:
    """In-process session factory."""

    def __init__(self) -> None:
        self._sessions: dict[str, EmbedSession] = {}
        self._seq = 0

    @property
    def session_ids(self) -> list[str]:
        return list(self._sessions)

    async def create_session(
        self,
        *,
        session_id: str | None = None,
        interceptor: ByteInterceptor | None = None,
        telnet_policy: TelnetPolicy | None = None,
        services: Mapping[str, Any] | None = None,
    ) -> EmbedSession:
        if not session_id:
            self._seq += 1
            session_id = f"embed-{self._seq:x}"
        if session_id in self._sessions:
            raise RuntimeError(f"session already exists: {session_id}")
        sess = EmbedSession(
            session_id,
            interceptor=interceptor,
            telnet_policy=telnet_policy,
            services=services,
        )
        self._sessions[session_id] = sess
        return sess

    def get_session(self, session_id: str) -> EmbedSession | None:
        return self._sessions.get(session_id)

    def remove_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
