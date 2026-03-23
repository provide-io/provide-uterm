#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Protocol types for terminal reader/writer pairs."""

from __future__ import annotations

from typing import Protocol


class TerminalReader(Protocol):
    """Structural type for an async byte-stream reader."""

    async def read(self, n: int) -> bytes: ...


class TerminalWriter(Protocol):
    """Structural type for an async byte-stream writer."""

    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def get_extra_info(self, key: str, default: object = None) -> object: ...

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...
