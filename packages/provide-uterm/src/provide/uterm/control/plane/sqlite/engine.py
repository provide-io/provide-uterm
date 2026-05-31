#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from provide.uterm.control.plane.sqlite.approval_store import SqliteApprovalStore
from provide.uterm.control.plane.sqlite.connection import SqliteConnectionError, connect_sqlite
from provide.uterm.control.plane.sqlite.lease_store import SqliteLeaseStore
from provide.uterm.control.plane.sqlite.migration import SqliteMigrationError, apply_migrations
from provide.uterm.control.plane.sqlite.session_store import SqliteSessionStore
from provide.uterm.control.plane.sqlite.token_store import SqliteTokenStore
from provide.uterm.control.plane.sqlite.transaction import SqliteTransaction

if TYPE_CHECKING:
    import aiosqlite

    from provide.uterm.control.plane import ControlPlaneConfig, EngineCapabilities


@dataclass(slots=True)
class SqliteControlPlane:
    """Inert SQLite-backed control-plane shell."""

    config: ControlPlaneConfig
    capabilities: EngineCapabilities = field(init=False)
    _conn: aiosqlite.Connection | None = field(init=False, default=None, repr=False)
    _tx_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock, repr=False)

    def __post_init__(self) -> None:
        self.capabilities = self.config.capabilities

    async def open(self) -> None:
        if self._conn is not None:
            return
        self._conn = await connect_sqlite(self.config.database_url)

    async def close(self) -> None:
        if self._conn is None:
            return
        await self._conn.close()
        self._conn = None

    async def migrate(self) -> None:
        try:
            await self.open()
            assert self._conn is not None
            async with self._tx_lock:
                await apply_migrations(self._conn)
        except SqliteMigrationError:
            raise
        except SqliteConnectionError as exc:
            raise SqliteMigrationError(f"failed to apply control-plane migration: {exc}") from exc

    async def begin(self) -> SqliteTransaction:
        await self.open()
        assert self._conn is not None
        await self._tx_lock.acquire()
        try:
            await self._conn.execute("BEGIN IMMEDIATE")
        except Exception:
            self._tx_lock.release()
            raise

        async def _release_lock() -> None:
            if self._tx_lock.locked():
                self._tx_lock.release()

        return SqliteTransaction(self._conn, on_close=_release_lock)

    async def reap(self, *, now: float, retention_s: int) -> int:
        """Physically delete control-plane rows whose soft-delete/expiry timestamp
        is older than ``now - retention_s``, then truncate the WAL.  Returns the
        total number of rows deleted."""
        cutoff = now - retention_s
        tx = await self.begin()
        try:
            assert self._conn is not None
            deleted = 0
            for sql, params in (
                (
                    "DELETE FROM cp_resume_tokens WHERE (revoked_at IS NOT NULL AND revoked_at < ?) OR expires_at < ?",
                    (cutoff, cutoff),
                ),
                (
                    "DELETE FROM cp_session_tokens "
                    "WHERE (revoked_at IS NOT NULL AND revoked_at < ?) "
                    "OR (expires_at IS NOT NULL AND expires_at < ?)",
                    (cutoff, cutoff),
                ),
                ("DELETE FROM cp_sessions WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,)),
                ("DELETE FROM cp_leases WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,)),
                ("DELETE FROM cp_approvals WHERE resolved_at IS NOT NULL AND resolved_at < ?", (cutoff,)),
            ):
                cursor = await self._conn.execute(sql, params)
                deleted += cursor.rowcount
                await cursor.close()
            await tx.commit()
        except Exception:
            await tx.rollback()
            raise
        # WAL checkpoint must run OUTSIDE the BEGIN IMMEDIATE txn (lock re-acquired).
        async with self._tx_lock:
            assert self._conn is not None
            await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return deleted

    def session_store(self, tx: SqliteTransaction) -> SqliteSessionStore:
        return SqliteSessionStore(tx)

    def token_store(self, tx: SqliteTransaction) -> SqliteTokenStore:
        return SqliteTokenStore(tx)

    def approval_store(self, tx: SqliteTransaction) -> SqliteApprovalStore:
        return SqliteApprovalStore(tx)

    def lease_store(self, tx: SqliteTransaction) -> SqliteLeaseStore:
        return SqliteLeaseStore(tx)
