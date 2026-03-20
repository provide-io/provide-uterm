#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Resume token store for WebSocket session resumption.

When a browser WS drops, its role and hijack ownership are lost.  The resume
token store allows a reconnecting browser to prove it was the same session
and reclaim its previous role / hijack ownership within a configurable TTL.

Two implementations are provided:

* :class:`InMemoryResumeStore` — lightweight, single-process, no dependencies.
* ``SqliteResumeStore`` (CF package) — durable, backed by DO SQLite.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from provide.telemetry import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class ResumeSession:
    """State preserved for a disconnected browser session."""

    token: str
    worker_id: str
    role: str
    created_at: float  # time.monotonic()
    expires_at: float  # time.monotonic()
    was_hijack_owner: bool = False


@runtime_checkable
class ResumeTokenStore(Protocol):
    """Abstract interface for resume token persistence."""

    def create(self, worker_id: str, role: str, ttl_s: float) -> str:
        """Create a new resume token and return it."""
        ...

    def get(self, token: str) -> ResumeSession | None:
        """Look up a token, returning ``None`` if expired or not found."""
        ...

    def mark_hijack_owner(self, token: str, is_owner: bool) -> None:
        """Flag that the session held (or lost) hijack ownership at disconnect."""
        ...

    def revoke(self, token: str) -> None:
        """Invalidate a token immediately (e.g. after successful resume)."""
        ...


class InMemoryResumeStore:
    """In-memory resume token store with automatic expiry pruning.

    Suitable for single-process deployments.  Tokens are pruned lazily on
    :meth:`get` and eagerly via :meth:`cleanup_expired`.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, ResumeSession] = {}
        # Reverse mapping: allows disconnect handler to find a token by WS identity
        # without scanning all tokens.  Managed externally by TermHub via
        # _ws_to_resume_token.

    def create(self, worker_id: str, role: str, ttl_s: float) -> str:
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        self._tokens[token] = ResumeSession(
            token=token,
            worker_id=worker_id,
            role=role,
            created_at=now,
            expires_at=now + ttl_s,
        )
        return token

    def get(self, token: str) -> ResumeSession | None:
        session = self._tokens.get(token)
        if session is None:
            return None
        if time.monotonic() > session.expires_at:
            del self._tokens[token]
            return None
        return session

    def mark_hijack_owner(self, token: str, is_owner: bool) -> None:
        session = self._tokens.get(token)
        if session is not None:
            session.was_hijack_owner = is_owner

    def revoke(self, token: str) -> None:
        self._tokens.pop(token, None)

    def cleanup_expired(self) -> int:
        """Remove all expired tokens. Returns the number of tokens removed."""
        now = time.monotonic()
        expired = [t for t, s in self._tokens.items() if now > s.expires_at]
        for t in expired:
            del self._tokens[t]
        return len(expired)

    def __len__(self) -> int:
        return len(self._tokens)

    def active_tokens(self) -> dict[str, Any]:
        """Return a snapshot of all non-expired tokens (for diagnostics)."""
        now = time.monotonic()
        return {t: s for t, s in self._tokens.items() if now <= s.expires_at}
