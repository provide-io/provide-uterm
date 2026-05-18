#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Resume token store for WebSocket session resumption.

When a browser WS drops, its role and hijack ownership are lost. The resume
token store allows a reconnecting browser to prove it was the same session
and reclaim its previous role / hijack ownership within a configurable TTL.

Two implementations are provided:

* :class:`InMemoryResumeStore` - lightweight, single-process, no dependencies.
* :class:`ControlPlaneResumeStore` - bridge adapter backed by the async
  control-plane token store.
"""

from __future__ import annotations

import secrets
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from provide.uterm.control.plane.token.types import ResumeTokenRecord

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass(slots=True)
class ResumeSession:
    """State preserved for a disconnected browser session."""

    token: str
    worker_id: str
    role: str
    created_at: float  # time.monotonic()
    expires_at: float  # time.monotonic()
    was_hijack_owner: bool = False
    wall_created_at: float = 0.0  # time.time() at token creation, for session identity checks


@runtime_checkable
class ResumeTokenStore(Protocol):
    """Abstract interface for async resume token persistence."""

    async def create(self, worker_id: str, role: str, ttl_s: float) -> str:
        """Create a new resume token and return it."""

    async def get(self, token: str) -> ResumeSession | None:
        """Look up a token, returning ``None`` if expired or not found."""

    async def mark_hijack_owner(self, token: str, is_owner: bool) -> None:
        """Flag that the session held (or lost) hijack ownership at disconnect."""

    async def revoke(self, token: str) -> None:
        """Invalidate a token immediately (e.g. after successful resume)."""


class InMemoryResumeStore:
    """In-memory resume token store with automatic expiry pruning.

    Suitable for single-process deployments. Tokens are pruned lazily on
    :meth:`get` and eagerly via :meth:`cleanup_expired`.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, ResumeSession] = {}
        # Reverse mapping: allows disconnect handler to find a token by WS identity
        # without scanning all tokens. Managed externally by TermHub via
        # _ws_to_resume_token.

    async def create(self, worker_id: str, role: str, ttl_s: float) -> str:
        # Opportunistically prune on create so expired entries cannot grow
        # without bound in long-lived processes with repeated browser churn.
        self.cleanup_expired()
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        self._tokens[token] = ResumeSession(
            token=token,
            worker_id=worker_id,
            role=role,
            created_at=now,
            expires_at=now + ttl_s,
            wall_created_at=time.time(),
        )
        return token

    async def get(self, token: str) -> ResumeSession | None:
        session = self._tokens.get(token)
        if session is None:
            return None
        if time.monotonic() > session.expires_at:
            del self._tokens[token]
            return None
        return session

    async def mark_hijack_owner(self, token: str, is_owner: bool) -> None:
        session = self._tokens.get(token)
        if session is not None:
            session.was_hijack_owner = is_owner

    async def revoke(self, token: str) -> None:
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


@runtime_checkable
class _ControlPlaneResumeTokenStore(Protocol):
    async def create_resume_token(self, record: Any) -> None: ...

    async def get_resume_token(self, token_value: str) -> Any | None: ...

    async def revoke_resume_token(self, token_value: str, revoked_at: float) -> None: ...


@runtime_checkable
class _ControlPlaneResumeBackend(Protocol):
    async def begin(self) -> Any: ...

    def token_store(self, tx: Any) -> _ControlPlaneResumeTokenStore: ...


class ControlPlaneResumeStore:
    """Async resume token store backed by the control-plane token store."""

    def __init__(self, control_plane: _ControlPlaneResumeBackend) -> None:
        self._control_plane = control_plane
        self._created_at_mono: dict[str, float] = {}

    async def _run_tx(self, op: Callable[[Any], Awaitable[Any]]) -> Any:
        tx = await self._control_plane.begin()
        store = self._control_plane.token_store(tx)
        try:
            result = await op(store)
        except Exception:
            with suppress(Exception):
                await tx.rollback()
            raise
        await tx.commit()
        return result

    async def create(self, worker_id: str, role: str, ttl_s: float) -> str:
        token = secrets.token_urlsafe(32)
        created_at_mono = time.monotonic()
        created_at_wall = time.time()
        record = _make_resume_record(
            token_value=token,
            session_id=worker_id,
            role=role,
            created_at=created_at_wall,
            expires_at=created_at_wall + ttl_s,
            was_hijack_owner=False,
            revoked_at=None,
        )

        async def _op(store: _ControlPlaneResumeTokenStore) -> None:
            await store.create_resume_token(record)

        await self._run_tx(_op)
        self._created_at_mono[token] = created_at_mono
        return token

    async def get(self, token: str) -> ResumeSession | None:
        async def _op(store: _ControlPlaneResumeTokenStore) -> ResumeSession | None:
            record = await store.get_resume_token(token)
            if record is None:
                self._created_at_mono.pop(token, None)
                return None
            now_wall = time.time()
            now_mono = time.monotonic()
            if now_wall > float(record.expires_at):
                await store.revoke_resume_token(token, time.time())
                self._created_at_mono.pop(token, None)
                return None
            age_s = max(0.0, now_wall - float(record.created_at))
            created_at = self._created_at_mono.get(token, now_mono - age_s)
            expires_at = now_mono + max(0.0, float(record.expires_at) - now_wall)
            return ResumeSession(
                token=str(record.token_value),
                worker_id=str(record.session_id),
                role=str(record.role),
                created_at=created_at,
                expires_at=expires_at,
                was_hijack_owner=bool(record.was_hijack_owner),
                wall_created_at=float(record.created_at),
            )

        result: ResumeSession | None = await self._run_tx(_op)
        return result

    async def mark_hijack_owner(self, token: str, is_owner: bool) -> None:
        async def _op(store: _ControlPlaneResumeTokenStore) -> None:
            record = await store.get_resume_token(token)
            if record is None:
                return
            await store.create_resume_token(replace(record, was_hijack_owner=is_owner))

        await self._run_tx(_op)

    async def revoke(self, token: str) -> None:
        async def _op(store: _ControlPlaneResumeTokenStore) -> None:
            record = await store.get_resume_token(token)
            if record is None:
                self._created_at_mono.pop(token, None)
                return
            await store.revoke_resume_token(token, time.time())
            self._created_at_mono.pop(token, None)

        await self._run_tx(_op)


def _make_resume_record(
    *,
    token_value: str,
    session_id: str,
    role: str,
    created_at: float,
    expires_at: float,
    was_hijack_owner: bool,
    revoked_at: float | None,
) -> ResumeTokenRecord:
    return ResumeTokenRecord(
        token_value=token_value,
        session_id=session_id,
        role=role,
        created_at=created_at,
        expires_at=expires_at,
        was_hijack_owner=was_hijack_owner,
        revoked_at=revoked_at,
    )
