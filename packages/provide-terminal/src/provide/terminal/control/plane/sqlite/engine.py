from __future__ import annotations

import asyncio
import aiosqlite
from dataclasses import dataclass, field

from provide.terminal.control.plane import ControlPlaneConfig, EngineCapabilities
from provide.terminal.control.plane.sqlite.connection import SqliteConnectionError, connect_sqlite
from provide.terminal.control.plane.sqlite.migration import SqliteMigrationError, apply_migrations
from provide.terminal.control.plane.sqlite.approval_store import SqliteApprovalStore
from provide.terminal.control.plane.sqlite.lease_store import SqliteLeaseStore
from provide.terminal.control.plane.sqlite.session_store import SqliteSessionStore
from provide.terminal.control.plane.sqlite.transaction import SqliteTransaction
from provide.terminal.control.plane.sqlite.token_store import SqliteTokenStore


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

    def session_store(self, tx: SqliteTransaction) -> SqliteSessionStore:
        return SqliteSessionStore(tx)

    def token_store(self, tx: SqliteTransaction) -> SqliteTokenStore:
        return SqliteTokenStore(tx)

    def approval_store(self, tx: SqliteTransaction) -> SqliteApprovalStore:
        return SqliteApprovalStore(tx)

    def lease_store(self, tx: SqliteTransaction) -> SqliteLeaseStore:
        return SqliteLeaseStore(tx)
