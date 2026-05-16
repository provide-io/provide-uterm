from __future__ import annotations

from typing import Protocol


class Transaction(Protocol):
    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...
