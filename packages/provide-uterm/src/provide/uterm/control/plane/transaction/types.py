#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from typing import Protocol


class Transaction(Protocol):
    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...
