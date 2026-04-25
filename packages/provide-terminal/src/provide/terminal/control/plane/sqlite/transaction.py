from __future__ import annotations

import aiosqlite
from collections.abc import Awaitable, Callable

from provide.terminal.control.plane.transaction import Transaction


class SqliteTransaction(Transaction):
    def __init__(self, conn: aiosqlite.Connection, on_close: Callable[[], Awaitable[None]] | None = None) -> None:
        self._conn = conn
        self._on_close = on_close
        self._closed = False

    async def commit(self) -> None:
        if not self._closed:
            try:
                await self._conn.commit()
            finally:
                self._closed = True
                if self._on_close is not None:
                    await self._on_close()

    async def rollback(self) -> None:
        if not self._closed:
            try:
                await self._conn.rollback()
            finally:
                self._closed = True
                if self._on_close is not None:
                    await self._on_close()
